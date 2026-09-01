"""Canonical, strict contracts for published-port intent and plan evidence."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from ipaddress import ip_address, ip_network
from pathlib import Path

import pytest

from scripts import published_ports as pp
from scripts.published_port_contracts import (
    CanonicalContractError,
    PublishedPortIntentV1,
    PublishedPortObservedListenerV1,
    PublishedPortPlanReceiptV1,
    PublishedPortPlanV1,
    PublishedPortPrestateV1,
    PublishedPortProjectContainerV1,
    verify_receipt_for_plan,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE_SHA = "a" * 40
TARGET_ID = "1" * 64
APP_ID = "2" * 64


def _canonical(payload: object) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()


@pytest.fixture(scope="module")
def intent() -> PublishedPortIntentV1:
    declared = pp.plan(pp.load_declaration(), "freeradius", "production")
    return PublishedPortIntentV1.from_declared(declared)


@pytest.fixture(scope="module")
def plan(intent: PublishedPortIntentV1) -> PublishedPortPlanV1:
    return PublishedPortPlanV1(
        source_sha=SOURCE_SHA,
        target_server_name="dotmac-sub-prod",
        change_reference="INC-9001",
        reason="Remove undeclared IPv6 listeners",
        declaration_digest=f"sha256:{'3' * 64}",
        compose_digest=f"sha256:{'4' * 64}",
        intent=intent,
        prestate=PublishedPortPrestateV1(
            target_container_id=TARGET_ID,
            target_image_digest=f"sha256:{'5' * 64}",
            listeners=(
                PublishedPortObservedListenerV1(
                    container_port=1812,
                    host_ip=ip_address("0.0.0.0"),
                    host_port=1812,
                    protocol="udp",
                ),
            ),
            non_port_definition_digest=f"sha256:{'6' * 64}",
            project_containers=(
                PublishedPortProjectContainerV1(
                    service="app", container="dotmac_sub_app", container_id=APP_ID
                ),
                PublishedPortProjectContainerV1(
                    service="freeradius",
                    container="dotmac_sub_freeradius",
                    container_id=TARGET_ID,
                ),
            ),
        ),
    )


def test_intent_uses_real_ip_and_network_types(intent: PublishedPortIntentV1) -> None:
    target = intent.targets[0]
    assert target.bind == ip_address("0.0.0.0")
    assert target.expected_listeners == (ip_address("0.0.0.0"),)
    assert target.required_clients == (
        ip_network("102.220.189.0/24"),
        ip_network("160.119.127.0/24"),
    )


def test_plan_round_trips_only_from_canonical_bytes(plan: PublishedPortPlanV1) -> None:
    raw = plan.canonical_bytes()
    assert raw.endswith(b"\n")
    assert PublishedPortPlanV1.from_canonical_bytes(raw) == plan
    assert plan.canonical_digest() == f"sha256:{hashlib.sha256(raw).hexdigest()}"


def test_mapping_order_does_not_change_canonical_bytes(
    plan: PublishedPortPlanV1,
) -> None:
    payload = plan.model_dump(mode="json", by_alias=True)
    payload["intent"]["assignments"] = dict(
        reversed(list(payload["intent"]["assignments"].items()))
    )
    rebuilt = PublishedPortPlanV1.from_canonical_bytes(_canonical(payload))
    assert rebuilt.canonical_bytes() == plan.canonical_bytes()


def test_pretty_but_valid_json_is_refused(plan: PublishedPortPlanV1) -> None:
    pretty = json.dumps(plan.model_dump(mode="json", by_alias=True), indent=2).encode()
    with pytest.raises(CanonicalContractError, match="non-canonical"):
        PublishedPortPlanV1.from_canonical_bytes(pretty)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("unknown",), "field"),
        (("intent", "unknown"), "field"),
        (("prestate", "unknown"), "field"),
        (("schema",), "PublishedPortPlanV2"),
        (("repository",), "someone/else"),
        (("workflow",), ".github/workflows/infrastructure-reconcile-apply.yml"),
        (("protected_ref",), "refs/heads/dev"),
        (("source_sha",), "short"),
        (("target_server_name",), "some-other-host"),
        (("declaration_digest",), "3" * 64),
        (("intent", "targets", 0, "host_port"), "1812"),
        (("intent", "targets", 0, "bind"), "not-an-address"),
        (("intent", "targets", 0, "required_clients"), ["not-a-network"]),
    ],
)
def test_plan_parser_refuses_unknown_identity_coercion_and_bad_networks(
    plan: PublishedPortPlanV1,
    path: tuple[object, ...],
    value: object,
) -> None:
    payload = plan.model_dump(mode="json", by_alias=True)
    cursor = payload
    for component in path[:-1]:
        cursor = cursor[component]
    cursor[path[-1]] = value
    with pytest.raises(CanonicalContractError):
        PublishedPortPlanV1.from_canonical_bytes(_canonical(payload))


def test_plan_refuses_unsorted_project_map(plan: PublishedPortPlanV1) -> None:
    payload = plan.model_dump(mode="json", by_alias=True)
    payload["prestate"]["project_containers"].reverse()
    with pytest.raises(CanonicalContractError, match="sorted"):
        PublishedPortPlanV1.from_canonical_bytes(_canonical(payload))


def test_intent_refuses_a_target_bind_env_without_an_assignment(
    intent: PublishedPortIntentV1,
) -> None:
    payload = intent.model_dump(mode="json", by_alias=True)
    payload["targets"][0]["bind_env"] = "OTHER_BIND"
    with pytest.raises(CanonicalContractError, match="assignment keys"):
        PublishedPortIntentV1.from_canonical_bytes(_canonical(payload))


def test_intent_refuses_an_assignment_that_disagrees_with_target_bind(
    intent: PublishedPortIntentV1,
) -> None:
    payload = intent.model_dump(mode="json", by_alias=True)
    payload["assignments"]["FREERADIUS_BIND"] = "127.0.0.1:"
    with pytest.raises(CanonicalContractError, match="must agree"):
        PublishedPortIntentV1.from_canonical_bytes(_canonical(payload))


def test_prestate_refuses_an_unknown_non_port_projection_schema(
    plan: PublishedPortPlanV1,
) -> None:
    payload = plan.model_dump(mode="json", by_alias=True)
    payload["prestate"]["non_port_projection"] = "UnversionedProjection"
    with pytest.raises(CanonicalContractError):
        PublishedPortPlanV1.from_canonical_bytes(_canonical(payload))


def _changed_digest(plan: PublishedPortPlanV1, mutate) -> str:
    payload = plan.model_dump(mode="json", by_alias=True)
    mutate(payload)
    return PublishedPortPlanV1.from_canonical_bytes(
        _canonical(payload)
    ).canonical_digest()


def test_plan_digest_is_sensitive_to_every_mutable_binding(
    plan: PublishedPortPlanV1,
) -> None:
    mutations = (
        lambda p: p.update({"source_sha": "b" * 40}),
        lambda p: p.update({"change_reference": "CHG-2"}),
        lambda p: p.update({"reason": "Different reviewed reason"}),
        lambda p: p.update({"declaration_digest": f"sha256:{'7' * 64}"}),
        lambda p: p.update({"compose_digest": f"sha256:{'8' * 64}"}),
        _change_assignment_and_bind,
        _change_target_socket,
        _change_bind_env,
        lambda p: p["prestate"].update({"target_image_digest": f"sha256:{'9' * 64}"}),
        lambda p: p["prestate"].update(
            {"non_port_definition_digest": f"sha256:{'a' * 64}"}
        ),
        lambda p: p["prestate"]["listeners"][0].update({"host_ip": "127.0.0.1"}),
        _change_target_container,
        lambda p: p["prestate"]["project_containers"][0].update(
            {"container_id": "d" * 64}
        ),
    )
    digests = {_changed_digest(plan, mutation) for mutation in mutations}
    assert plan.canonical_digest() not in digests
    assert len(digests) == len(mutations)


def _change_bind_env(payload: dict[str, object]) -> None:
    value = payload["intent"]["assignments"].pop("FREERADIUS_BIND")
    payload["intent"]["assignments"]["RADIUS_PUBLISH_BIND"] = value
    for target in payload["intent"]["targets"]:
        target["bind_env"] = "RADIUS_PUBLISH_BIND"


def _change_assignment_and_bind(payload: dict[str, object]) -> None:
    payload["intent"]["assignments"]["FREERADIUS_BIND"] = "127.0.0.1:"
    for target in payload["intent"]["targets"]:
        target["bind"] = "127.0.0.1"
        target["expected_listeners"] = ["127.0.0.1"]


def _change_target_socket(payload: dict[str, object]) -> None:
    target = payload["intent"]["targets"][-1]
    target["host_port"] = 2823
    target["key"] = "freeradius:2823/udp"


def _change_target_container(payload: dict[str, object]) -> None:
    new_id = "c" * 64
    payload["prestate"]["target_container_id"] = new_id
    target = next(
        row
        for row in payload["prestate"]["project_containers"]
        if row["service"] == "freeradius"
    )
    target["container_id"] = new_id


def test_receipt_round_trips_and_binds_full_plan(plan: PublishedPortPlanV1) -> None:
    receipt = PublishedPortPlanReceiptV1.for_plan(plan=plan, run_id=123)
    parsed = PublishedPortPlanReceiptV1.from_canonical_bytes(receipt.canonical_bytes())
    assert parsed == receipt
    verify_receipt_for_plan(receipt, plan)
    assert receipt.plan_digest == plan.canonical_digest()
    assert receipt.prestate_digest == plan.prestate_digest()


def test_two_run_receipts_share_plan_but_not_run_identity(
    plan: PublishedPortPlanV1,
) -> None:
    first = PublishedPortPlanReceiptV1.for_plan(plan=plan, run_id=123)
    second = PublishedPortPlanReceiptV1.for_plan(plan=plan, run_id=456)
    assert first != second
    assert first.plan_digest == second.plan_digest
    assert first.prestate_digest == second.prestate_digest


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repository", "someone/else"),
        ("run_attempt", 2),
        ("run_id", "123"),
        ("artifact_file", "other.json"),
        ("plan_digest", "e" * 64),
    ],
)
def test_receipt_refuses_wrong_identity_rerun_and_coercion(
    plan: PublishedPortPlanV1, field: str, value: object
) -> None:
    receipt = PublishedPortPlanReceiptV1.for_plan(plan=plan, run_id=123)
    payload = receipt.model_dump(mode="json", by_alias=True)
    payload[field] = value
    with pytest.raises(CanonicalContractError):
        PublishedPortPlanReceiptV1.from_canonical_bytes(_canonical(payload))


def test_receipt_refuses_extra_fields(plan: PublishedPortPlanV1) -> None:
    receipt = PublishedPortPlanReceiptV1.for_plan(plan=plan, run_id=123)
    payload = receipt.model_dump(mode="json", by_alias=True)
    payload["authorization"] = True
    with pytest.raises(CanonicalContractError):
        PublishedPortPlanReceiptV1.from_canonical_bytes(_canonical(payload))


def test_receipt_with_different_plan_digest_is_refused(
    plan: PublishedPortPlanV1,
) -> None:
    receipt = PublishedPortPlanReceiptV1.for_plan(plan=plan, run_id=123)
    changed = receipt.model_copy(update={"plan_digest": f"sha256:{'f' * 64}"})
    with pytest.raises(CanonicalContractError, match="exact plan bytes"):
        verify_receipt_for_plan(changed, plan)


def test_cli_plan_emits_canonical_intent_not_an_apply_plan() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.published_ports",
            "plan",
            "--service",
            "postgres-local",
            "--environment",
            "production",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    parsed = PublishedPortIntentV1.from_canonical_bytes(result.stdout)
    assert parsed.service == "postgres-local"
    assert parsed.assignments == {"PG_LOCAL_BIND": "0.0.0.0:"}
    assert json.loads(result.stdout)["schema"] == "PublishedPortIntentV1"
