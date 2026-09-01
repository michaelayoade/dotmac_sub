"""Behavioral proofs for the plan/apply/deadman published-port seam."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from scripts import published_port_deadman as deadman
from scripts import published_port_plan_observer as observer
from scripts import published_port_reconcile_v2 as owner
from scripts.published_port_contracts import (
    CanonicalContractError,
    PublishedPortApplyAdmissionV2,
    PublishedPortClientReachProofV2,
    PublishedPortDeadmanStateV2,
    PublishedPortExecutionPlanV2,
    PublishedPortFirewallProofV2,
    PublishedPortHostSnapshotV2,
    PublishedPortPlanRunObservationV2,
)

SOURCE_SHA = "a" * 40
TARGET_BEFORE = "1" * 64
TARGET_AFTER = "2" * 64
APP_ID = "3" * 64
IMAGE_ID = f"sha256:{'4' * 64}"
IMAGE_REFERENCE = f"ghcr.io/dotmac/freeradius@sha256:{'5' * 64}"
NOW = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
ADAPTER = (
    Path(__file__).resolve().parents[1] / "scripts/reconcile_published_ports_v2.sh"
)


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def test_toolchain_metadata_guard_refuses_a_writable_dependency() -> None:
    source = ADAPTER.read_text(encoding="utf-8")
    start = source.index("require_root_owned_nonwritable_metadata() {")
    finish = source.index("\n}\n", start) + 3
    guard = source[start:finish]
    harness = f"die() {{ exit 73; }}\n{guard}\n"

    safe = subprocess.run(
        [
            "bash",
            "-c",
            f'{harness}require_root_owned_nonwritable_metadata "0:644" dependency',
        ],
        check=False,
    )
    writable = subprocess.run(
        [
            "bash",
            "-c",
            f'{harness}require_root_owned_nonwritable_metadata "0:666" dependency',
        ],
        check=False,
    )
    assert safe.returncode == 0
    assert writable.returncode == 73


def _listener_rows(*, include_ipv6: bool, container_id: str) -> list[dict[str, object]]:
    ports: dict[str, list[dict[str, str]]] = {}
    for port in (1812, 1813, 1822, 1823):
        bindings = [{"HostIp": "0.0.0.0", "HostPort": str(port)}]
        if include_ipv6:
            bindings.append({"HostIp": "::", "HostPort": str(port)})
        ports[f"{port}/udp"] = bindings
    return [
        {
            "compose_project": "dotmac_sub",
            "service": "app",
            "container": "/dotmac_sub_app",
            "container_id": APP_ID,
            "image_id": f"sha256:{'6' * 64}",
            "image_reference": f"ghcr.io/dotmac/sub@sha256:{'7' * 64}",
            "ports": {},
        },
        {
            "compose_project": "dotmac_sub",
            "service": "freeradius",
            "container": "/dotmac_sub_freeradius",
            "container_id": container_id,
            "image_id": IMAGE_ID,
            "image_reference": IMAGE_REFERENCE,
            "ports": ports,
        },
    ]


def _write_containers(
    path: Path, *, include_ipv6: bool, container_id: str = TARGET_BEFORE
) -> None:
    path.write_text(
        "".join(
            json.dumps(row, sort_keys=True) + "\n"
            for row in _listener_rows(
                include_ipv6=include_ipv6, container_id=container_id
            )
        ),
        encoding="utf-8",
    )


def _effective(*, admitted_ports: bool = False) -> dict[str, object]:
    ports = []
    if admitted_ports:
        ports = [
            {
                "host_ip": "0.0.0.0",
                "published": port,
                "protocol": "udp",
                "target": port,
            }
            for port in (1812, 1813, 1822, 1823)
        ]
    return {
        "services": {
            "freeradius": {
                "command": ["freeradius", "-f"],
                "environment": {"RADIUS_SECRET": "not-serialized-by-owner"},
                "image": IMAGE_REFERENCE,
                "ports": ports,
                "restart": "unless-stopped",
            }
        }
    }


@pytest.fixture()
def execution(tmp_path: Path) -> PublishedPortExecutionPlanV2:
    containers = tmp_path / "containers.jsonl"
    effective = tmp_path / "effective.json"
    _write_containers(containers, include_ipv6=True)
    _write_json(effective, _effective())
    result = owner.build_execution_plan(
        service="freeradius",
        source_sha=SOURCE_SHA,
        target_server_name="dotmac-sub-prod",
        change_reference="CHG-9001",
        reason="Remove the undeclared IPv6 publishes without changing the image",
        effective_compose=effective,
        containers=containers,
    )
    assert len(result.client_obligations) == 8
    assert {str(row.client_network) for row in result.client_obligations} == {
        "102.220.189.0/24",
        "160.119.127.0/24",
    }
    return result


def _write_artifact(
    directory: Path,
    execution: PublishedPortExecutionPlanV2,
    run_id: int,
    *,
    planned_at: datetime = NOW,
) -> None:
    directory.mkdir()
    owner.write_plan_artifacts(
        execution_plan=execution,
        run_id=run_id,
        planned_at=planned_at,
        output_dir=directory,
    )


def _observation(
    run_id: int, *, completed_at: datetime = NOW
) -> PublishedPortPlanRunObservationV2:
    return PublishedPortPlanRunObservationV2(
        source_sha=SOURCE_SHA,
        run_id=run_id,
        created_at=NOW - timedelta(minutes=2),
        completed_at=completed_at,
    )


def _write_contract(path: Path, value: object) -> None:
    path.write_bytes(value.canonical_bytes())


@pytest.fixture()
def admitted(
    tmp_path: Path, execution: PublishedPortExecutionPlanV2
) -> PublishedPortApplyAdmissionV2:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write_artifact(first, execution, 101)
    _write_artifact(second, execution, 202)
    first_observation = tmp_path / "first-observation.json"
    second_observation = tmp_path / "second-observation.json"
    _write_contract(first_observation, _observation(101))
    _write_contract(second_observation, _observation(202))
    return owner.admit_apply(
        first_dir=first,
        second_dir=second,
        first_observation_path=first_observation,
        second_observation_path=second_observation,
        source_sha=SOURCE_SHA,
        apply_run_id=303,
        admitted_at=NOW + timedelta(minutes=1),
        expected_plan_digest=execution.canonical_digest(),
        firewall_verifier_identity="service:sub-firewall-verifier",
        client_collector_identity="service:sub-external-reach-collector",
    )


def test_execution_plan_is_canonical_and_refuses_extra_or_coerced_fields(
    execution: PublishedPortExecutionPlanV2,
) -> None:
    assert (
        PublishedPortExecutionPlanV2.from_canonical_bytes(execution.canonical_bytes())
        == execution
    )
    payload = execution.model_dump(mode="json", by_alias=True)
    payload["authorization"] = True
    raw = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    with pytest.raises(CanonicalContractError):
        PublishedPortExecutionPlanV2.from_canonical_bytes(raw)
    payload.pop("authorization")
    payload["plan"]["prestate"]["project_containers"][0]["container_id"] = 123
    raw = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    with pytest.raises(CanonicalContractError):
        PublishedPortExecutionPlanV2.from_canonical_bytes(raw)


def test_root_observer_emits_only_safe_snapshot_and_read_only_docker_calls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    execution: PublishedPortExecutionPlanV2,
) -> None:
    config: dict[str, object] = {
        "schema": "PublishedPortObserverConfigV1",
        "target_server_name": "dotmac-sub-prod",
        "compose_project": "dotmac_sub",
        "docker_bin": "/usr/bin/docker",
        "deploy_dir": "/opt/dotmac/sub",
        "env_file": "/opt/dotmac/sub/.env",
        "compose_files": ["/opt/dotmac/sub/docker-compose.yml"],
        "allowed_services": ["freeradius", "postgres-local"],
    }
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> object:
        calls.append(command)
        if command[-2:] == ["ps", "-q"]:
            return type("Result", (), {"stdout": f"{TARGET_BEFORE}\n{APP_ID}\n"})()
        if command[:2] == ["/usr/bin/docker", "inspect"]:
            rows = _listener_rows(include_ipv6=True, container_id=TARGET_BEFORE)
            return type(
                "Result",
                (),
                {"stdout": "".join(json.dumps(row) + "\n" for row in rows)},
            )()
        return type("Result", (), {"stdout": json.dumps(_effective())})()

    monkeypatch.setattr(observer, "_load_config", lambda _path: config)
    monkeypatch.setattr(observer.subprocess, "run", fake_run)
    snapshot = PublishedPortHostSnapshotV2.from_canonical_bytes(
        observer._canonical(observer.collect("freeradius"))
    )
    assert snapshot.service == "freeradius"
    assert snapshot.observer_digest.startswith("sha256:")
    assert len(snapshot.containers) == 2
    assert all("Env" not in json.dumps(row) for row in snapshot.model_dump(mode="json"))
    assert any(command[-2:] == ["ps", "-q"] for command in calls)
    assert any(command[:2] == ["/usr/bin/docker", "inspect"] for command in calls)
    assert any(command[-3:] == ["config", "--format", "json"] for command in calls)
    assert all(
        not ({"up", "pull", "build", "restart", "stop"} & set(command))
        for command in calls
    )
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_bytes(snapshot.canonical_bytes())
    rebuilt = owner.build_execution_plan_from_snapshot(
        service="freeradius",
        source_sha=SOURCE_SHA,
        target_server_name="dotmac-sub-prod",
        change_reference="CHG-9001",
        reason="Remove the undeclared IPv6 publishes without changing the image",
        snapshot_path=snapshot_path,
    )
    assert rebuilt.canonical_bytes() == execution.canonical_bytes()


def test_admission_requires_two_distinct_terminal_first_attempt_receipts(
    admitted: PublishedPortApplyAdmissionV2,
) -> None:
    assert tuple(row.run_id for row in admitted.plan_runs) == (101, 202)
    assert len(set(admitted.artifact_receipt_digests)) == 2
    assert admitted.workflow.endswith("infrastructure-reconcile-apply.yml")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("protected_ref", "refs/heads/feature"),
        ("run_attempt", 2),
        ("status", "queued"),
        ("conclusion", "failure"),
        ("source_sha", "b" * 40),
    ],
)
def test_terminal_observation_refuses_mutable_failed_rerun_or_wrong_source(
    field: str, value: object
) -> None:
    payload = _observation(101).model_dump(mode="json", by_alias=True)
    payload[field] = value
    with pytest.raises(ValidationError):
        PublishedPortPlanRunObservationV2.model_validate(payload, strict=True)


def test_admission_refuses_duplicate_plan_run(
    tmp_path: Path, execution: PublishedPortExecutionPlanV2
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write_artifact(first, execution, 101)
    _write_artifact(second, execution, 101)
    first_obs = tmp_path / "one.json"
    second_obs = tmp_path / "two.json"
    _write_contract(first_obs, _observation(101))
    _write_contract(second_obs, _observation(101))
    with pytest.raises(owner.ReconcileV2Error, match="same run"):
        owner.admit_apply(
            first_dir=first,
            second_dir=second,
            first_observation_path=first_obs,
            second_observation_path=second_obs,
            source_sha=SOURCE_SHA,
            apply_run_id=303,
            admitted_at=NOW + timedelta(minutes=1),
            expected_plan_digest=execution.canonical_digest(),
            firewall_verifier_identity="firewall",
            client_collector_identity="reach",
        )


def test_admission_refuses_expired_plan(
    tmp_path: Path, execution: PublishedPortExecutionPlanV2
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write_artifact(first, execution, 101)
    _write_artifact(second, execution, 202)
    first_obs = tmp_path / "one.json"
    second_obs = tmp_path / "two.json"
    _write_contract(first_obs, _observation(101))
    _write_contract(second_obs, _observation(202))
    with pytest.raises(owner.ReconcileV2Error, match="expired"):
        owner.admit_apply(
            first_dir=first,
            second_dir=second,
            first_observation_path=first_obs,
            second_observation_path=second_obs,
            source_sha=SOURCE_SHA,
            apply_run_id=303,
            admitted_at=NOW + timedelta(hours=2),
            expected_plan_digest=execution.canonical_digest(),
            firewall_verifier_identity="firewall",
            client_collector_identity="reach",
        )


def test_admission_refuses_two_different_plan_byte_sequences(
    tmp_path: Path, execution: PublishedPortExecutionPlanV2
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write_artifact(first, execution, 101)
    changed = execution.model_copy(
        update={"plan": execution.plan.model_copy(update={"reason": "another reason"})}
    )
    _write_artifact(second, changed, 202)
    first_obs = tmp_path / "one.json"
    second_obs = tmp_path / "two.json"
    _write_contract(first_obs, _observation(101))
    _write_contract(second_obs, _observation(202))
    with pytest.raises(owner.ReconcileV2Error, match="byte-identical"):
        owner.admit_apply(
            first_dir=first,
            second_dir=second,
            first_observation_path=first_obs,
            second_observation_path=second_obs,
            source_sha=SOURCE_SHA,
            apply_run_id=303,
            admitted_at=NOW + timedelta(minutes=1),
            expected_plan_digest=execution.canonical_digest(),
            firewall_verifier_identity="firewall",
            client_collector_identity="reach",
        )


def test_immediate_third_plan_must_remain_byte_identical(
    tmp_path: Path,
    execution: PublishedPortExecutionPlanV2,
    admitted: PublishedPortApplyAdmissionV2,
) -> None:
    plan_path = tmp_path / "plan.json"
    immediate_path = tmp_path / "immediate.json"
    admission_path = tmp_path / "admission.json"
    _write_contract(plan_path, execution)
    _write_contract(immediate_path, execution)
    _write_contract(admission_path, admitted)
    assert (
        owner.verify_third_plan(
            plan_path,
            immediate_path,
            admission_path,
            now=NOW + timedelta(minutes=2),
        )
        == execution
    )
    changed = execution.model_copy(
        update={"plan": execution.plan.model_copy(update={"reason": "state drift"})}
    )
    _write_contract(immediate_path, changed)
    with pytest.raises(owner.ReconcileV2Error, match="under-lock replan"):
        owner.verify_third_plan(
            plan_path,
            immediate_path,
            admission_path,
            now=NOW + timedelta(minutes=2),
        )


def _proofs(
    tmp_path: Path,
    execution: PublishedPortExecutionPlanV2,
    admission: PublishedPortApplyAdmissionV2,
) -> tuple[list[Path], list[Path]]:
    firewall_paths = []
    reach_paths = []
    for index, obligation in enumerate(execution.client_obligations):
        firewall = PublishedPortFirewallProofV2(
            operation_id=admission.operation_id,
            execution_plan_digest=execution.canonical_digest(),
            target_key=obligation.target_key,
            client_network=obligation.client_network,
            verifier_identity=admission.firewall_verifier_identity,
            ruleset_digest=f"sha256:{index:064x}",
            observed_at=NOW + timedelta(minutes=2),
        )
        reach = PublishedPortClientReachProofV2(
            operation_id=admission.operation_id,
            execution_plan_digest=execution.canonical_digest(),
            target_key=obligation.target_key,
            client_network=obligation.client_network,
            collector_identity=admission.client_collector_identity,
            collector_evidence_digest=f"sha256:{index + 20:064x}",
            observed_at=NOW + timedelta(minutes=2),
        )
        firewall_path = tmp_path / f"firewall-{index}.json"
        reach_path = tmp_path / f"reach-{index}.json"
        _write_contract(firewall_path, firewall)
        _write_contract(reach_path, reach)
        firewall_paths.append(firewall_path)
        reach_paths.append(reach_path)
    return firewall_paths, reach_paths


def _poststate(tmp_path: Path) -> tuple[Path, Path]:
    containers = tmp_path / "post-containers.jsonl"
    effective = tmp_path / "post-effective.json"
    _write_containers(containers, include_ipv6=False, container_id=TARGET_AFTER)
    _write_json(effective, _effective(admitted_ports=True))
    return containers, effective


def test_postconditions_prove_both_families_and_every_external_obligation(
    tmp_path: Path,
    execution: PublishedPortExecutionPlanV2,
    admitted: PublishedPortApplyAdmissionV2,
) -> None:
    containers, effective = _poststate(tmp_path)
    firewall, reach = _proofs(tmp_path, execution, admitted)
    verdict = owner.verify_postconditions(
        execution=execution,
        admission=admitted,
        effective_compose=effective,
        containers=containers,
        firewall_paths=firewall,
        reach_paths=reach,
        now=NOW + timedelta(minutes=3),
    )
    assert tuple(row.family for row in verdict.address_families) == ("ipv4", "ipv6")
    assert verdict.address_families[0].expected
    assert verdict.address_families[1].expected == ()
    assert len(verdict.firewall_proof_digests) == 8
    assert len(verdict.client_reach_proof_digests) == 8


def test_postconditions_refuse_missing_extra_or_untrusted_external_evidence(
    tmp_path: Path,
    execution: PublishedPortExecutionPlanV2,
    admitted: PublishedPortApplyAdmissionV2,
) -> None:
    containers, effective = _poststate(tmp_path)
    firewall, reach = _proofs(tmp_path, execution, admitted)
    with pytest.raises(owner.ReconcileV2Error, match="exactly cover"):
        owner.verify_postconditions(
            execution=execution,
            admission=admitted,
            effective_compose=effective,
            containers=containers,
            firewall_paths=firewall[:-1],
            reach_paths=reach,
            now=NOW + timedelta(minutes=3),
        )
    with pytest.raises(owner.ReconcileV2Error, match="exactly cover"):
        owner.verify_postconditions(
            execution=execution,
            admission=admitted,
            effective_compose=effective,
            containers=containers,
            firewall_paths=firewall,
            reach_paths=reach[:-1],
            now=NOW + timedelta(minutes=3),
        )
    extra = tmp_path / "extra.json"
    extra.write_bytes(firewall[0].read_bytes())
    with pytest.raises(owner.ReconcileV2Error, match="duplicate proof"):
        owner.verify_postconditions(
            execution=execution,
            admission=admitted,
            effective_compose=effective,
            containers=containers,
            firewall_paths=[*firewall, extra],
            reach_paths=reach,
            now=NOW + timedelta(minutes=3),
        )
    payload = json.loads(firewall[0].read_bytes())
    payload["verifier_identity"] = "untrusted"
    firewall[0].write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(owner.ReconcileV2Error, match="unadmitted verifier"):
        owner.verify_postconditions(
            execution=execution,
            admission=admitted,
            effective_compose=effective,
            containers=containers,
            firewall_paths=firewall,
            reach_paths=reach,
            now=NOW + timedelta(minutes=3),
        )


def test_postconditions_refuse_non_target_identity_non_port_and_ipv6_drift(
    tmp_path: Path,
    execution: PublishedPortExecutionPlanV2,
    admitted: PublishedPortApplyAdmissionV2,
) -> None:
    containers, effective = _poststate(tmp_path)
    firewall, reach = _proofs(tmp_path, execution, admitted)
    rows = _listener_rows(include_ipv6=False, container_id=TARGET_AFTER)
    rows[0]["container_id"] = "8" * 64
    containers.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    with pytest.raises(owner.ReconcileV2Error, match="non-target"):
        owner.verify_postconditions(
            execution=execution,
            admission=admitted,
            effective_compose=effective,
            containers=containers,
            firewall_paths=firewall,
            reach_paths=reach,
            now=NOW + timedelta(minutes=3),
        )
    _write_containers(containers, include_ipv6=False, container_id=TARGET_AFTER)
    rows = _listener_rows(include_ipv6=False, container_id=TARGET_AFTER)
    rows[1]["image_id"] = f"sha256:{'9' * 64}"
    containers.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    with pytest.raises(owner.ReconcileV2Error, match="exact running image ID"):
        owner.verify_postconditions(
            execution=execution,
            admission=admitted,
            effective_compose=effective,
            containers=containers,
            firewall_paths=firewall,
            reach_paths=reach,
            now=NOW + timedelta(minutes=3),
        )
    _write_containers(containers, include_ipv6=False, container_id=TARGET_AFTER)
    changed = _effective(admitted_ports=True)
    changed["services"]["freeradius"]["command"] = ["freeradius", "-X"]
    _write_json(effective, changed)
    with pytest.raises(owner.ReconcileV2Error, match="non-port"):
        owner.verify_postconditions(
            execution=execution,
            admission=admitted,
            effective_compose=effective,
            containers=containers,
            firewall_paths=firewall,
            reach_paths=reach,
            now=NOW + timedelta(minutes=3),
        )
    _write_json(effective, _effective(admitted_ports=True))
    _write_containers(containers, include_ipv6=True, container_id=TARGET_AFTER)
    with pytest.raises(ValidationError, match="address-family"):
        owner.verify_postconditions(
            execution=execution,
            admission=admitted,
            effective_compose=effective,
            containers=containers,
            firewall_paths=firewall,
            reach_paths=reach,
            now=NOW + timedelta(minutes=3),
        )


def test_deadman_restores_env_and_target_with_no_pull_build_or_dependencies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    execution: PublishedPortExecutionPlanV2,
    admitted: PublishedPortApplyAdmissionV2,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "APP_ENV=production\nFREERADIUS_BIND=127.0.0.1:\n", encoding="utf-8"
    )
    env_file.chmod(0o640)
    compose = tmp_path / "compose.yml"
    compose.write_text("services: {}\n", encoding="utf-8")
    state = owner.prepare_deadman_state(
        admission=admitted,
        execution=execution,
        env_file=env_file,
        docker_bin=Path("/usr/bin/python3"),
        deploy_dir=tmp_path,
        compose_files=(compose,),
        deadline=NOW + timedelta(minutes=5),
        now=NOW,
    )
    state_root = tmp_path / "state"
    operation_dir = state_root / admitted.operation_id
    operation_dir.mkdir(parents=True)
    state_path = operation_dir / "state.json"
    state_path.write_bytes(state.canonical_bytes())
    env_file.write_text(
        "APP_ENV=production\nFREERADIUS_BIND=0.0.0.0:\n", encoding="utf-8"
    )
    commands: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> object:
        commands.append(command)
        if command[:2] == ["/usr/bin/python3", "inspect"]:
            ports: dict[str, list[dict[str, str]]] = {}
            for row in execution.plan.prestate.listeners:
                key = f"{row.container_port}/{row.protocol}"
                ports.setdefault(key, []).append(
                    {"HostIp": str(row.host_ip), "HostPort": str(row.host_port)}
                )
            return type(
                "Result",
                (),
                {
                    "stdout": json.dumps(
                        {
                            "image_id": IMAGE_ID,
                            "image_reference": IMAGE_REFERENCE,
                            "ports": ports,
                        }
                    )
                },
            )()
        if "ps" in command:
            return type("Result", (), {"stdout": f"{'9' * 64}\n"})()
        return type("Result", (), {"stdout": ""})()

    monkeypatch.setattr(deadman, "STATE_DIR", state_root)
    monkeypatch.setattr(deadman.subprocess, "run", fake_run)
    deadman.rollback_now(admitted.operation_id, "signal")
    assert "FREERADIUS_BIND=127.0.0.1:" in env_file.read_text(encoding="utf-8")
    assert env_file.stat().st_mode & 0o777 == 0o640
    up = next(command for command in commands if "up" in command)
    assert up[-1] == "freeradius"
    assert "--no-deps" in up
    assert "--no-build" in up
    assert up[up.index("--pull") + 1] == "never"
    assert "--force-recreate" in up
    terminal = PublishedPortDeadmanStateV2.from_canonical_bytes(state_path.read_bytes())
    assert terminal.state == "rolled_back"
    assert terminal.state_reason == "signal"


def test_deadman_timeout_and_failed_disarm_are_falsifiable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    execution: PublishedPortExecutionPlanV2,
    admitted: PublishedPortApplyAdmissionV2,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("FREERADIUS_BIND=127.0.0.1:\n", encoding="utf-8")
    compose = tmp_path / "compose.yml"
    compose.write_text("services: {}\n", encoding="utf-8")
    state = owner.prepare_deadman_state(
        admission=admitted,
        execution=execution,
        env_file=env_file,
        docker_bin=Path("/usr/bin/python3"),
        deploy_dir=tmp_path,
        compose_files=(compose,),
        deadline=NOW + timedelta(minutes=1),
        now=NOW,
    ).model_copy(update={"deadline": datetime(2020, 1, 1, tzinfo=UTC)})
    state_root = tmp_path / "state"
    operation_dir = state_root / admitted.operation_id
    operation_dir.mkdir(parents=True)
    state_path = operation_dir / "state.json"
    state_path.write_bytes(state.canonical_bytes())
    monkeypatch.setattr(deadman, "STATE_DIR", state_root)
    monkeypatch.setattr(deadman, "_restore_env", lambda _document: None)
    monkeypatch.setattr(
        deadman,
        "_observed_target",
        lambda _document: (
            IMAGE_ID,
            IMAGE_REFERENCE,
            [
                (row.container_port, str(row.host_ip), row.host_port, row.protocol)
                for row in execution.plan.prestate.listeners
            ],
        ),
    )
    monkeypatch.setattr(deadman.subprocess, "run", lambda *_args, **_kwargs: None)
    deadman.check(admitted.operation_id)
    terminal = PublishedPortDeadmanStateV2.from_canonical_bytes(state_path.read_bytes())
    assert terminal.state_reason == "timeout"
    with pytest.raises(deadman.DeadmanError, match="only an armed"):
        deadman.disarm(admitted.operation_id)


def test_fake_reboot_process_reloads_persistent_state_and_rolls_back_timeout(
    tmp_path: Path,
    execution: PublishedPortExecutionPlanV2,
    admitted: PublishedPortApplyAdmissionV2,
) -> None:
    """Rehearsed, not proved-live: a fresh process consumes the persisted state."""

    env_file = tmp_path / ".env"
    env_file.write_text("FREERADIUS_BIND=127.0.0.1:\n", encoding="utf-8")
    compose = tmp_path / "compose.yml"
    compose.write_text("services: {}\n", encoding="utf-8")
    state = owner.prepare_deadman_state(
        admission=admitted,
        execution=execution,
        env_file=env_file,
        docker_bin=Path("/usr/bin/python3"),
        deploy_dir=tmp_path,
        compose_files=(compose,),
        deadline=NOW + timedelta(minutes=1),
        now=NOW,
    ).model_copy(update={"deadline": datetime(2020, 1, 1, tzinfo=UTC)})
    state_root = tmp_path / "state"
    operation_dir = state_root / admitted.operation_id
    operation_dir.mkdir(parents=True)
    state_path = operation_dir / "state.json"
    state_path.write_bytes(state.canonical_bytes())
    listener_rows = [
        [row.container_port, str(row.host_ip), row.host_port, row.protocol]
        for row in execution.plan.prestate.listeners
    ]
    program = """
import json
import sys
from pathlib import Path
from scripts import published_port_deadman as deadman
deadman.STATE_DIR = Path(sys.argv[1])
deadman._restore_env = lambda _document: None
deadman.subprocess.run = lambda *_args, **_kwargs: None
listeners = [tuple(row) for row in json.loads(sys.argv[5])]
deadman._observed_target = lambda _document: (sys.argv[3], sys.argv[4], listeners)
deadman.check(sys.argv[2])
"""
    subprocess.run(
        [
            sys.executable,
            "-c",
            program,
            str(state_root),
            admitted.operation_id,
            IMAGE_ID,
            IMAGE_REFERENCE,
            json.dumps(listener_rows),
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
    )
    terminal = PublishedPortDeadmanStateV2.from_canonical_bytes(state_path.read_bytes())
    assert terminal.state == "rolled_back"
    assert terminal.state_reason == "timeout"
