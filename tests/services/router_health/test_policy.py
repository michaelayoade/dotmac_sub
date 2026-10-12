from __future__ import annotations

import copy
import json
import uuid
from datetime import date

from app.services.router_health.policy import (
    RouterHealthPolicy,
    effective_policy,
    parse_policy,
    router_health_policy_error,
    unmatched_entries,
)
from tests.services.router_health.support import FIXTURE_DIR, TODAY, default_policy

EXAMPLE = (
    FIXTURE_DIR.parents[2] / "docs" / "network" / "router_health_policy.example.json"
)


def base() -> dict:
    return default_policy().model_dump(mode="json")


def entry(**overrides) -> dict:
    data = {"match": {"identity": "A"}}
    data.update(overrides)
    return data


def error(raw: dict) -> str | None:
    return router_health_policy_error(raw, today=TODAY)


def test_minimal_default_and_none_are_valid() -> None:
    assert error({"version": 1}) is None
    assert router_health_policy_error(None) is None
    assert router_health_policy_error([1]) is not None


def test_example_json_validates_and_uses_documentation_addresses() -> None:
    raw = json.loads(EXAMPLE.read_text())
    assert error(raw) is None
    for source in raw["defaults"]["snmp_allowed_sources"]:
        assert source.startswith("192.0.2.")


def test_missing_reason_is_refused() -> None:
    raw = base()
    raw["routers"] = [
        entry(input_v4={"mode": "observe", "until": "2026-10-18", "reason": ""})
    ]
    assert error(raw)
    raw["routers"] = [
        entry(exceptions=[{"check": "x", "until": "2026-10-18", "reason": " "}])
    ]
    assert error(raw)
    raw["routers"] = [entry(role="core")]
    assert error(raw)


def test_until_over_180_days_is_refused() -> None:
    raw = base()
    raw["routers"] = [
        entry(
            exceptions=[{"check": "x", "until": "2027-06-01", "reason": "r"}],
        )
    ]
    assert "180" in (error(raw) or "")


def test_weakening_override_needs_until() -> None:
    raw = base()
    raw["routers"] = [entry(input_v4={"mode": "observe", "reason": "r"})]
    assert error(raw)


def test_permanent_only_for_regex_overrides() -> None:
    raw = base()
    raw["routers"] = [
        entry(input_v4={"mode": "observe", "permanent": True, "reason": "r"})
    ]
    assert error(raw)
    raw["routers"] = [
        entry(
            input_v6={
                "mode": "custom",
                "accept_comment_prefix": "V6:",
                "final_drop_comment_regex": "(?i)^V6: drop$",
                "permanent": True,
                "reason": "r",
            }
        )
    ]
    assert error(raw) is None


def test_snmp_sources_refuse_open_networks_and_junk() -> None:
    for bad in ("0.0.0.0/0", "::/0", "not-a-cidr"):
        raw = copy.deepcopy(base())
        raw["defaults"]["snmp_allowed_sources"] = [bad]
        assert error(raw), bad


def test_regex_backreference_and_length_refused() -> None:
    raw = base()
    raw["defaults"]["input_v4"]["final_drop_comment_regex"] = r"(a)\1"
    assert error(raw)
    raw = base()
    raw["defaults"]["input_v4"]["final_drop_comment_regex"] = "a" * 201
    assert error(raw)


def test_duplicate_matches_refused() -> None:
    raw = base()
    raw["routers"] = [
        entry(match={"identity": "Ilupeju Access "}),
        entry(match={"identity": "ilupeju   access"}),
    ]
    assert error(raw)


def test_match_needs_a_key_and_all_given_keys_must_agree() -> None:
    raw = base()
    raw["routers"] = [entry(match={})]
    assert error(raw)
    policy = RouterHealthPolicy.model_validate(
        {**base(), "routers": [entry(match={"identity": "A", "router_name": "r1"})]}
    )
    miss = effective_policy(
        policy, identity="A", router_name="other", router_id=uuid.uuid4(), today=TODAY
    )
    hit = effective_policy(
        policy, identity="a", router_name="r1", router_id=uuid.uuid4(), today=TODAY
    )
    assert not miss.matched_entry and hit.matched_entry


def test_unknown_check_off_name_refused() -> None:
    raw = base()
    raw["roles"] = {"core": {"checks_off": ["not-a-check"]}}
    assert error(raw)


def test_unmatched_entry_is_reported() -> None:
    policy = RouterHealthPolicy.model_validate(
        {**base(), "routers": [entry(match={"identity": "Gone"})]}
    )
    rid = uuid.uuid4()
    assert len(unmatched_entries(policy, [("Present", "Present", rid)])) == 1
    assert unmatched_entries(policy, [("Gone", "Gone", rid)]) == []


def test_invalid_stored_policy_falls_back_to_defaults_with_error() -> None:
    loaded = parse_policy({"version": 1, "routers": [entry(role="core")]}, today=TODAY)
    assert loaded.error
    assert loaded.policy.routers == []


def test_lapsed_exception_is_listed_and_no_longer_active() -> None:
    policy = RouterHealthPolicy.model_validate(
        {
            **base(),
            "routers": [
                entry(
                    exceptions=[
                        {
                            "check": "ntp-client-off",
                            "until": "2026-10-12",
                            "reason": "r",
                        }
                    ]
                )
            ],
        }
    )
    on_day = effective_policy(
        policy,
        identity="A",
        router_name="r",
        router_id=uuid.uuid4(),
        today=date(2026, 10, 12),
    )
    next_day = effective_policy(
        policy,
        identity="A",
        router_name="r",
        router_id=uuid.uuid4(),
        today=date(2026, 10, 13),
    )
    assert on_day.excepted("ntp-client-off") and not on_day.lapsed
    assert not next_day.excepted("ntp-client-off") and len(next_day.lapsed) == 1
