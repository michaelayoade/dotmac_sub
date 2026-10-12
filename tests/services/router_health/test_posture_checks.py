from __future__ import annotations

import uuid
from datetime import date

from app.services.router_health.contracts import CheckId, HealthPriority
from app.services.router_health.policy import (
    RouterHealthPolicy,
    effective_policy,
    parse_policy,
)
from app.services.router_health.posture_checks import evaluate_posture
from tests.services.router_health.support import (
    INPUT_V4_OK,
    TODAY,
    build_export,
    default_policy,
    effective,
    parsed,
)


def run(text: str, eff=None, *, nas: bool = False):
    return evaluate_posture(
        parsed(text),
        eff or effective(),
        router_label="Test",
        suspended_list="suspended",
        has_pppoe_or_nas=nas,
    )


def checks(result) -> dict[CheckId, HealthPriority]:
    return {f.check: f.priority for f in result.findings}


def test_healthy_router_has_no_findings() -> None:
    assert run(build_export()).findings == ()


def test_input_v4_missing_is_high_for_access() -> None:
    result = run(build_export(v4=None))
    assert checks(result)[CheckId.input_v4_missing] is HealthPriority.high


def test_input_v4_requires_final_drop_without_matchers() -> None:
    bad = INPUT_V4_OK.replace(
        'add action=drop chain=input comment="INPUT-PROTECT: drop all else"',
        'add action=drop chain=input comment="INPUT-PROTECT: drop all else" protocol=tcp',
    )
    assert CheckId.input_v4_missing in checks(run(build_export(v4=bad)))


def test_input_v4_bypass_unconditional_accept_before_final_drop() -> None:
    bad = "add action=accept chain=input\n" + INPUT_V4_OK
    assert (
        checks(run(build_export(v4=bad)))[CheckId.input_v4_bypass]
        is HealthPriority.high
    )


def test_disabled_bypass_rule_is_ignored() -> None:
    ok = "add action=accept chain=input disabled=yes\n" + INPUT_V4_OK
    assert run(build_export(v4=ok)).findings == ()


def test_border_observe_with_counter_passes_and_without_is_medium() -> None:
    policy = parse_policy(
        {
            **default_policy().model_dump(mode="json"),
            "routers": [
                {
                    "match": {"identity": "Test Access"},
                    "role": "border",
                    "role_reason": "border",
                    "input_v4": {
                        "mode": "observe",
                        "until": "2026-10-18",
                        "reason": "observe window",
                    },
                }
            ],
        },
        today=TODAY,
    ).policy
    eff = effective(policy)
    counter = INPUT_V4_OK.rsplit("\n", 1)[0] + (
        '\nadd action=passthrough chain=input comment="INPUT-PROTECT: count would-drop"'
    )
    assert run(build_export(v4=counter), eff).findings == ()
    result = run(build_export(v4=None), eff)
    assert checks(result)[CheckId.input_v4_missing] is HealthPriority.medium


def test_observe_exception_expires_on_the_day_after_until() -> None:
    raw = {
        "version": 1,
        "routers": [
            {
                "match": {"identity": "Test Access"},
                "input_v4": {"mode": "observe", "until": "2026-10-18", "reason": "r"},
            }
        ],
    }
    policy = parse_policy(raw, today=TODAY).policy
    before = effective(policy, today=date(2026, 10, 18))
    after = effective(policy, today=date(2026, 10, 19))
    assert before.input_v4.mode.value == "observe"
    assert after.input_v4.mode.value == "enforce"
    assert [item.kind for item in after.lapsed] == ["input_v4"]


def test_input_v6_absent_section_fails_medium() -> None:
    result = run(build_export(v6=None))
    assert checks(result)[CheckId.input_v6_missing] is HealthPriority.medium


def test_snmp_writable_is_critical() -> None:
    snmp = (
        "/snmp\nset enabled=yes\n/snmp community\n"
        "set [ find default=yes ] addresses=192.0.2.1/32 write-access=yes\n"
    )
    assert (
        checks(run(build_export(snmp=snmp)))[CheckId.snmp_exposed]
        is HealthPriority.critical
    )


def test_snmp_implicit_default_community_is_open() -> None:
    result = run(build_export(snmp="/snmp\nset enabled=yes\n"))
    assert checks(result)[CheckId.snmp_exposed] is HealthPriority.high


def test_snmp_superset_allowlist_fails_and_subset_passes() -> None:
    wide = "/snmp\nset enabled=yes\n/snmp community\nset [ find default=yes ] addresses=192.0.2.0/24\n"
    assert CheckId.snmp_exposed in checks(run(build_export(snmp=wide)))
    narrow = "/snmp\nset enabled=yes\n/snmp community\nset [ find default=yes ] addresses=192.0.2.1/32,192.0.2.2/32\n"
    assert CheckId.snmp_exposed not in checks(run(build_export(snmp=narrow)))


def test_snmp_disabled_is_skipped() -> None:
    assert CheckId.snmp_exposed not in checks(run(build_export(snmp="")))


def test_snmp_evidence_never_contains_community_names() -> None:
    snmp = (
        "/snmp\nset enabled=yes\n/snmp community\n"
        "add name=SENTINEL-COMMUNITY-NAME addresses=203.0.113.0/24\n"
    )
    result = run(build_export(snmp=snmp))
    assert "SENTINEL-COMMUNITY-NAME" not in repr(result)


def test_reverse_proxy_rules_by_version() -> None:
    enabled = build_export(version="7.22.2", services="/ip service\nset ssh port=120\n")
    assert CheckId.reverse_proxy_enabled in checks(run(enabled))
    old = build_export(version="7.13.2", services="/ip service\nset ssh port=120\n")
    assert CheckId.reverse_proxy_enabled not in checks(run(old))
    off = build_export(version="7.22.2")
    assert CheckId.reverse_proxy_enabled not in checks(run(off))


def test_ntp_both_server_syntaxes_and_failure() -> None:
    legacy = "/system ntp client\nset enabled=yes servers=192.0.2.9\n"
    assert CheckId.ntp_client_off not in checks(run(build_export(ntp=legacy)))
    assert CheckId.ntp_client_off not in checks(run(build_export()))
    assert CheckId.ntp_client_off in checks(run(build_export(ntp=None)))
    assert CheckId.ntp_client_off in checks(
        run(build_export(ntp="/system ntp client\nset enabled=yes\n"))
    )


def test_radius_incoming_absent_fails() -> None:
    result = run(build_export(radius_incoming=None))
    assert checks(result)[CheckId.radius_incoming_off] is HealthPriority.high


def test_suspension_jump_to_legacy_chain_is_critical() -> None:
    legacy = """\
add action=jump chain=forward comment="old" jump-target=splynx-blocked src-address-list=suspended
add action=accept chain=splynx-blocked comment="portal" dst-port=80 protocol=tcp
"""
    result = run(build_export(suspension=legacy))
    assert checks(result)[CheckId.suspension_jump] is HealthPriority.critical
    # the legacy chain is already reported by the jump finding
    assert CheckId.splynx_legacy_chain not in checks(result)


def test_missing_jump_is_critical_only_with_pppoe_or_nas() -> None:
    no_jump = build_export(suspension="", pppoe=False)
    assert CheckId.suspension_jump not in checks(run(no_jump))
    assert CheckId.suspension_jump in checks(run(no_jump, nas=True))
    assert CheckId.suspension_jump in checks(run(build_export(suspension="")))


def test_rate_limited_reject_only_chain_fails_final_drop_passes() -> None:
    limited = """\
add action=jump chain=forward jump-target=dotmac-wg src-address-list=suspended
add action=reject chain=dotmac-wg limit=10,5:packet reject-with=icmp-admin-prohibited
"""
    assert (
        checks(run(build_export(suspension=limited)))[CheckId.suspension_chain]
        is HealthPriority.critical
    )
    with_drop = limited + 'add action=drop chain=dotmac-wg comment="final"\n'
    assert CheckId.suspension_chain not in checks(
        run(build_export(suspension=with_drop))
    )


def test_unconditional_accept_before_jump_is_shadowed() -> None:
    shadow = "add action=accept chain=forward comment=all\n" + (
        "add action=jump chain=forward jump-target=dotmac-suspended src-address-list=suspended\n"
        "add action=drop chain=dotmac-suspended\n"
    )
    assert (
        checks(run(build_export(suspension=shadow)))[CheckId.suspension_shadowed]
        is HealthPriority.medium
    )
    guarded = (
        "add action=fasttrack-connection chain=forward connection-state=established,related\n"
        + SUSP
    )
    assert CheckId.suspension_shadowed not in checks(
        run(build_export(suspension=guarded))
    )


SUSP = """\
add action=jump chain=forward jump-target=dotmac-suspended src-address-list=suspended
add action=drop chain=dotmac-suspended
"""


def test_legacy_chain_levels_and_legacy_list() -> None:
    orphan = SUSP + "add action=accept chain=splynx-blocked\n"
    result = run(build_export(suspension=orphan))
    assert checks(result)[CheckId.splynx_legacy_chain] is HealthPriority.low
    used = SUSP + (
        "add action=jump chain=forward jump-target=splynx-x src-address-list=other\n"
        "add action=drop chain=splynx-x\n"
    )
    assert (
        checks(run(build_export(suspension=used)))[CheckId.splynx_legacy_chain]
        is HealthPriority.medium
    )
    listy = (
        "add action=jump chain=forward dst-address-list=!splynx-allowed-resources "
        "jump-target=dotmac-suspended src-address-list=suspended\n"
        "add action=drop chain=dotmac-suspended\n"
    )
    assert (
        checks(run(build_export(suspension=listy)))[CheckId.splynx_legacy_list]
        is HealthPriority.low
    )


def test_core_role_skips_suspension_and_radius_checks() -> None:
    policy = RouterHealthPolicy.model_validate(
        {
            **default_policy().model_dump(mode="json"),
            "roles": {
                "core": {
                    "checks_off": [
                        "suspension-jump",
                        "suspension-chain",
                        "radius-incoming",
                    ]
                }
            },
            "routers": [
                {
                    "match": {"identity": "Test Access"},
                    "role": "core",
                    "role_reason": "core",
                }
            ],
        }
    )
    eff = effective(policy)
    text = build_export(suspension="", radius_incoming=None)
    result = run(text, eff, nas=True)
    assert result.findings == ()
    assert CheckId.suspension_jump not in result.evaluated_checks


def test_dated_exception_suppresses_then_lapses() -> None:
    raw = {
        **default_policy().model_dump(mode="json"),
        "routers": [
            {
                "match": {"identity": "Test Access"},
                "exceptions": [
                    {"check": "ntp-client-off", "until": "2026-10-20", "reason": "r"}
                ],
            }
        ],
    }
    policy = RouterHealthPolicy.model_validate(raw)
    text = build_export(ntp=None)
    assert run(text, effective(policy, today=date(2026, 10, 20))).findings == ()
    assert CheckId.ntp_client_off in checks(
        run(text, effective(policy, today=date(2026, 10, 21)))
    )


def test_annotation_adds_known_issue_but_never_suppresses() -> None:
    raw = {
        **default_policy().model_dump(mode="json"),
        "routers": [
            {
                "match": {"identity": "Test Access"},
                "annotations": [{"check": "ntp-client-off", "note": "known upgrade"}],
            }
        ],
    }
    policy = RouterHealthPolicy.model_validate(raw)
    result = run(build_export(ntp=None), effective(policy))
    (finding,) = result.findings
    assert finding.known_issue == "known upgrade"


def test_unknown_version_does_not_evaluate_reverse_proxy() -> None:
    text = build_export().replace("by RouterOS 7.16.2", "by something")
    assert CheckId.reverse_proxy_enabled not in run(text).evaluated_checks


def test_effective_policy_without_entry_uses_strict_defaults() -> None:
    eff = effective_policy(
        RouterHealthPolicy(),
        identity="New",
        router_name="New",
        router_id=uuid.uuid4(),
        today=TODAY,
    )
    assert eff.role.value == "access"
    assert eff.input_v4.mode.value == "enforce"
    assert not eff.matched_entry
