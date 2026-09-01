"""Provider-neutral plan/admission/postcondition owner for port reconcile v2.

The shell and Actions workflows are adapters.  This module owns the immutable
decision/evidence boundaries and never invokes Docker, systemd, a firewall, or
the network.  In particular, PLAN is a pure reduction of files collected by a
read-only adapter; APPLY authority is a separate artifact and cannot be
manufactured by the planning path.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sys
from datetime import datetime, timedelta
from ipaddress import ip_address
from pathlib import Path
from typing import Never, TypeVar

from pydantic import ValidationError

from scripts.published_port_contracts import (
    AddressFamilyProofV2,
    CanonicalContractError,
    PublishedPortApplyAdmissionV2,
    PublishedPortApplyOutcomeV2,
    PublishedPortClientObligationV2,
    PublishedPortClientReachProofV2,
    PublishedPortContainerObservationV2,
    PublishedPortDeadmanStateV2,
    PublishedPortEnvPreimageV2,
    PublishedPortExecutionPlanV2,
    PublishedPortFirewallProofV2,
    PublishedPortHostSnapshotV2,
    PublishedPortIntentV1,
    PublishedPortObservedListenerV1,
    PublishedPortPlanArtifactReceiptV2,
    PublishedPortPlanReceiptV1,
    PublishedPortPlanRunObservationV2,
    PublishedPortPlanV1,
    PublishedPortPostconditionVerdictV2,
    PublishedPortPrestateV1,
    PublishedPortProjectContainerV1,
    StrictContract,
    verify_receipt_for_plan,
)
from scripts.published_ports import load_declaration, plan

REPO_ROOT = Path(__file__).resolve().parent.parent
DECLARATION = REPO_ROOT / "deploy" / "published_ports.toml"
COMPOSE = REPO_ROOT / "docker-compose.yml"
PLAN_OBSERVER = REPO_ROOT / "scripts" / "published_port_plan_observer.py"
MAX_PLAN_AGE = timedelta(hours=1)
MAX_PROOF_SKEW = timedelta(minutes=5)
ContractT = TypeVar("ContractT", bound=StrictContract)


class ReconcileV2Error(RuntimeError):
    """A typed v2 gate refused the operation."""


def _refuse(message: str) -> Never:
    raise ReconcileV2Error(message)


def _sha256_bytes(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("utf-8")


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        _refuse("timestamp must be an explicit UTC instant")
    return parsed


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReconcileV2Error(
            f"cannot read JSON document {path.name}: {error}"
        ) from error


def _read_contract(path: Path, contract: type[ContractT]) -> ContractT:
    try:
        return contract.from_canonical_bytes(path.read_bytes())
    except (OSError, CanonicalContractError) as error:
        raise ReconcileV2Error(f"invalid canonical {path.name}: {error}") from error


def _write_contract(path: Path, contract: StrictContract) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(contract.canonical_bytes())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _normalise_container_rows(
    path: Path,
) -> tuple[PublishedPortContainerObservationV2, ...]:
    rows: list[PublishedPortContainerObservationV2] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as error:
            raise ReconcileV2Error(
                f"invalid container observation at line {line_number}: {error}"
            ) from error
        allowed = {
            "compose_project",
            "service",
            "container",
            "container_id",
            "image_id",
            "image_reference",
            "ports",
        }
        if set(raw) != allowed:
            _refuse(
                "container observation must contain only the seven approved, "
                "secret-free fields"
            )
        listeners: list[PublishedPortObservedListenerV1] = []
        for port_spec, bindings in (raw["ports"] or {}).items():
            container_port_text, separator, protocol = port_spec.partition("/")
            if not separator or protocol not in {"tcp", "udp"}:
                _refuse(f"unsupported observed port key {port_spec!r}")
            for binding in bindings or ():
                if set(binding) != {"HostIp", "HostPort"}:
                    _refuse("observed port binding contains an unapproved field")
                listeners.append(
                    PublishedPortObservedListenerV1(
                        container_port=int(container_port_text),
                        host_ip=ip_address(binding["HostIp"]),
                        host_port=int(binding["HostPort"]),
                        protocol=protocol,
                    )
                )
        try:
            rows.append(
                PublishedPortContainerObservationV2(
                    compose_project=raw["compose_project"],
                    service=raw["service"],
                    container=str(raw["container"]).lstrip("/"),
                    container_id=str(raw["container_id"]).removeprefix("sha256:"),
                    image_id=raw["image_id"],
                    image_reference=raw["image_reference"],
                    listeners=tuple(
                        sorted(
                            listeners,
                            key=lambda item: (
                                item.container_port,
                                str(item.host_ip),
                                item.host_port,
                                item.protocol,
                            ),
                        )
                    ),
                )
            )
        except ValidationError as error:
            raise ReconcileV2Error(
                f"invalid container observation at line {line_number}: {error}"
            ) from error
    if not rows:
        _refuse("the compose project has no observed containers")
    rows.sort(key=lambda item: (item.service, item.container, item.container_id))
    if len({(row.service, row.container) for row in rows}) != len(rows):
        _refuse("container observations duplicate a compose service/container identity")
    return tuple(rows)


def _effective_service_projection(path: Path, service: str) -> tuple[str, str]:
    """Return (digest, image reference) without serializing the projection.

    The effective Compose document may contain resolved secret values.  It is
    read from a mode-0600 temporary file, reduced in memory, and only its digest
    leaves this function.  Ports are deliberately removed: they are the one
    axis this operation is permitted to change.
    """

    document = _read_json(path)
    if not isinstance(document, dict):
        _refuse("effective Compose document is not an object")
    services = document.get("services")
    if not isinstance(services, dict):
        _refuse("effective Compose document has no services object")
    definition = services.get(service)
    if not isinstance(definition, dict):
        _refuse(f"effective Compose has no service {service!r}")
    projection = dict(definition)
    projection.pop("ports", None)
    image = projection.get("image")
    if not isinstance(image, str) or not re.fullmatch(
        r"[^\s@]+@sha256:[0-9a-f]{64}", image
    ):
        _refuse("target service must resolve to an immutable digest-pinned image")
    return _sha256_bytes(_canonical_json(projection)), image


def build_execution_plan(
    *,
    service: str,
    source_sha: str,
    target_server_name: str,
    change_reference: str,
    reason: str,
    effective_compose: Path,
    containers: Path,
    declaration_path: Path = DECLARATION,
    compose_path: Path = COMPOSE,
) -> PublishedPortExecutionPlanV2:
    declared = plan(load_declaration(declaration_path), service, "production")
    intent = PublishedPortIntentV1.from_declared(declared)
    observations = _normalise_container_rows(containers)
    target_rows = tuple(row for row in observations if row.service == service)
    if len(target_rows) != 1:
        _refuse("exactly one running target service container is required")
    target = target_rows[0]
    non_port_digest, effective_image = _effective_service_projection(
        effective_compose, service
    )
    if effective_image != target.image_reference:
        _refuse("effective Compose image is not the exact running image reference")
    image_digest = target.image_reference.rsplit("@", 1)[1]
    base_plan = PublishedPortPlanV1(
        source_sha=source_sha,
        target_server_name=target_server_name,
        change_reference=change_reference,
        reason=reason,
        declaration_digest=sha256_file(declaration_path),
        compose_digest=sha256_file(compose_path),
        intent=intent,
        prestate=PublishedPortPrestateV1(
            target_container_id=target.container_id,
            target_image_digest=image_digest,
            listeners=target.listeners,
            non_port_definition_digest=non_port_digest,
            project_containers=tuple(
                PublishedPortProjectContainerV1(
                    service=row.service,
                    container=row.container,
                    container_id=row.container_id,
                )
                for row in observations
            ),
        ),
    )
    obligations = tuple(
        sorted(
            (
                PublishedPortClientObligationV2(
                    target_key=target_row.key,
                    client_network=network,
                )
                for target_row in intent.targets
                for network in target_row.required_clients
            ),
            key=lambda item: (item.target_key, str(item.client_network)),
        )
    )
    return PublishedPortExecutionPlanV2(
        plan=base_plan,
        plan_observer_digest=sha256_file(PLAN_OBSERVER),
        target_image_reference=target.image_reference,
        target_image_id=target.image_id,
        client_obligations=obligations,
    )


def build_execution_plan_from_snapshot(
    *,
    service: str,
    source_sha: str,
    target_server_name: str,
    change_reference: str,
    reason: str,
    snapshot_path: Path,
    declaration_path: Path = DECLARATION,
    compose_path: Path = COMPOSE,
) -> PublishedPortExecutionPlanV2:
    """Build from the safe output of the installed read-only observer."""

    snapshot = _read_contract(snapshot_path, PublishedPortHostSnapshotV2)
    if snapshot.service != service or snapshot.target_server_name != target_server_name:
        _refuse("host snapshot target coordinates differ from the plan request")
    declared = plan(load_declaration(declaration_path), service, "production")
    intent = PublishedPortIntentV1.from_declared(declared)
    target = next(row for row in snapshot.containers if row.service == service)
    if snapshot.effective_image_reference != target.image_reference:
        _refuse("snapshot effective image is not the exact running image reference")
    if snapshot.observer_digest != sha256_file(PLAN_OBSERVER):
        _refuse("installed PLAN observer differs from the protected source bytes")
    image_digest = target.image_reference.rsplit("@", 1)[1]
    base_plan = PublishedPortPlanV1(
        source_sha=source_sha,
        target_server_name=target_server_name,
        change_reference=change_reference,
        reason=reason,
        declaration_digest=sha256_file(declaration_path),
        compose_digest=sha256_file(compose_path),
        intent=intent,
        prestate=PublishedPortPrestateV1(
            target_container_id=target.container_id,
            target_image_digest=image_digest,
            listeners=target.listeners,
            non_port_definition_digest=snapshot.non_port_definition_digest,
            project_containers=tuple(
                PublishedPortProjectContainerV1(
                    service=row.service,
                    container=row.container,
                    container_id=row.container_id,
                )
                for row in snapshot.containers
            ),
        ),
    )
    obligations = tuple(
        sorted(
            (
                PublishedPortClientObligationV2(
                    target_key=target_row.key,
                    client_network=network,
                )
                for target_row in intent.targets
                for network in target_row.required_clients
            ),
            key=lambda item: (item.target_key, str(item.client_network)),
        )
    )
    return PublishedPortExecutionPlanV2(
        plan=base_plan,
        plan_observer_digest=snapshot.observer_digest,
        target_image_reference=target.image_reference,
        target_image_id=target.image_id,
        client_obligations=obligations,
    )


def write_plan_artifacts(
    *,
    execution_plan: PublishedPortExecutionPlanV2,
    run_id: int,
    planned_at: datetime,
    output_dir: Path,
) -> None:
    receipt_v1 = PublishedPortPlanReceiptV1.for_plan(
        plan=execution_plan.plan, run_id=run_id
    )
    artifact_receipt = PublishedPortPlanArtifactReceiptV2(
        receipt=receipt_v1,
        execution_plan_digest=execution_plan.canonical_digest(),
        planned_at=planned_at,
        expires_at=planned_at + MAX_PLAN_AGE,
    )
    _write_contract(output_dir / "plan.json", execution_plan)
    _write_contract(output_dir / "artifact-receipt.json", artifact_receipt)


def build_immediate_plan(
    *,
    basis: PublishedPortExecutionPlanV2,
    effective_compose: Path,
    containers: Path,
    declaration_path: Path = DECLARATION,
    compose_path: Path = COMPOSE,
) -> PublishedPortExecutionPlanV2:
    original = basis.plan
    return build_execution_plan(
        service=original.intent.service,
        source_sha=original.source_sha,
        target_server_name=original.target_server_name,
        change_reference=original.change_reference,
        reason=original.reason,
        effective_compose=effective_compose,
        containers=containers,
        declaration_path=declaration_path,
        compose_path=compose_path,
    )


def write_image_pin(execution: PublishedPortExecutionPlanV2, path: Path) -> None:
    document = {
        "services": {
            execution.plan.intent.service: {
                "image": execution.target_image_reference,
            }
        }
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_canonical_json(document))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def verify_plan_artifact(
    plan_path: Path, receipt_path: Path
) -> tuple[PublishedPortExecutionPlanV2, PublishedPortPlanArtifactReceiptV2]:
    execution = _read_contract(plan_path, PublishedPortExecutionPlanV2)
    receipt = _read_contract(receipt_path, PublishedPortPlanArtifactReceiptV2)
    verify_receipt_for_plan(receipt.receipt, execution.plan)
    if not hmac.compare_digest(
        receipt.execution_plan_digest, execution.canonical_digest()
    ):
        _refuse("artifact receipt does not bind the execution plan")
    return execution, receipt


def admit_apply(
    *,
    first_dir: Path,
    second_dir: Path,
    first_observation_path: Path,
    second_observation_path: Path,
    source_sha: str,
    apply_run_id: int,
    admitted_at: datetime,
    expected_plan_digest: str,
    firewall_verifier_identity: str,
    client_collector_identity: str,
) -> PublishedPortApplyAdmissionV2:
    first_plan, first_receipt = verify_plan_artifact(
        first_dir / "plan.json", first_dir / "artifact-receipt.json"
    )
    second_plan, second_receipt = verify_plan_artifact(
        second_dir / "plan.json", second_dir / "artifact-receipt.json"
    )
    if first_plan.canonical_bytes() != second_plan.canonical_bytes():
        _refuse("the two terminal plan decisions are not byte-identical")
    if not hmac.compare_digest(first_plan.canonical_digest(), expected_plan_digest):
        _refuse("reviewed apply digest differs from the two plan artifacts")
    if first_receipt.receipt.run_id == second_receipt.receipt.run_id:
        _refuse("the two plan artifacts came from the same run")
    observations = tuple(
        sorted(
            (
                _read_contract(
                    first_observation_path, PublishedPortPlanRunObservationV2
                ),
                _read_contract(
                    second_observation_path, PublishedPortPlanRunObservationV2
                ),
            ),
            key=lambda item: item.run_id,
        )
    )
    receipt_by_run = {
        first_receipt.receipt.run_id: first_receipt,
        second_receipt.receipt.run_id: second_receipt,
    }
    if set(receipt_by_run) != {observation.run_id for observation in observations}:
        _refuse("terminal run observations do not name the artifact run IDs")
    for observation in observations:
        receipt = receipt_by_run[observation.run_id]
        if observation.source_sha != receipt.receipt.source_sha:
            _refuse("terminal run source differs from its artifact receipt")
        if observation.completed_at > receipt.expires_at:
            _refuse("a plan run completed after its artifact freshness window")
        if observation.created_at > receipt.planned_at:
            _refuse("a plan artifact predates its workflow run")
        if observation.completed_at < receipt.planned_at:
            _refuse("a plan artifact was written after its run completed")
        if admitted_at > receipt.expires_at:
            _refuse("a plan artifact expired before apply admission")
    if first_plan.plan.source_sha != source_sha:
        _refuse("plan source differs from apply source")
    target_service = first_plan.plan.intent.service
    return PublishedPortApplyAdmissionV2(
        source_sha=source_sha,
        apply_run_id=apply_run_id,
        target_service=target_service,
        operation_id=f"port-{target_service}-{apply_run_id}",
        execution_plan_digest=first_plan.canonical_digest(),
        firewall_verifier_identity=firewall_verifier_identity,
        client_collector_identity=client_collector_identity,
        artifact_receipt_digests=tuple(
            sorted(
                (
                    first_receipt.canonical_digest(),
                    second_receipt.canonical_digest(),
                )
            )
        ),
        plan_runs=observations,
        admitted_at=admitted_at,
        expires_at=admitted_at + timedelta(minutes=30),
    )


def verify_admission(
    *,
    admission_path: Path,
    plan_path: Path,
    expected_source_sha: str,
    expected_apply_run_id: int,
    now: datetime,
) -> tuple[PublishedPortApplyAdmissionV2, PublishedPortExecutionPlanV2]:
    admission = _read_contract(admission_path, PublishedPortApplyAdmissionV2)
    execution = _read_contract(plan_path, PublishedPortExecutionPlanV2)
    admission.require_fresh(now)
    if admission.source_sha != expected_source_sha:
        _refuse("apply source SHA differs from the admitted source")
    if admission.apply_run_id != expected_apply_run_id:
        _refuse("apply run ID differs from the admitted run")
    if admission.target_service != execution.plan.intent.service:
        _refuse("admission target differs from the execution plan")
    if not hmac.compare_digest(
        admission.execution_plan_digest, execution.canonical_digest()
    ):
        _refuse("admission does not bind the exact execution plan")
    return admission, execution


def verify_third_plan(
    admitted_plan_path: Path,
    immediate_plan_path: Path,
    admission_path: Path,
    *,
    now: datetime,
) -> PublishedPortExecutionPlanV2:
    admitted = _read_contract(admitted_plan_path, PublishedPortExecutionPlanV2)
    immediate = _read_contract(immediate_plan_path, PublishedPortExecutionPlanV2)
    admission = _read_contract(admission_path, PublishedPortApplyAdmissionV2)
    admission.require_fresh(now)
    if admitted.canonical_bytes() != immediate.canonical_bytes():
        _refuse("immediate under-lock replan differs from the two admitted plans")
    if not hmac.compare_digest(
        admission.execution_plan_digest, immediate.canonical_digest()
    ):
        _refuse("immediate replan is not the separately admitted plan")
    return immediate


def _env_rows(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def _env_value(rows: list[str], key: str) -> tuple[bool, str]:
    matches = [line.partition("=")[2] for line in rows if line.startswith(f"{key}=")]
    if len(matches) > 1:
        _refuse(f"environment file contains duplicate {key} rows")
    return (bool(matches), matches[0] if matches else "")


def _atomic_env_update(path: Path, assignments: dict[str, str]) -> None:
    stat = path.stat()
    rows = _env_rows(path)
    for key in assignments:
        _env_value(rows, key)
    retained = [
        line
        for line in rows
        if not any(line.startswith(f"{key}=") for key in assignments)
    ]
    retained.extend(f"{key}={assignments[key]}" for key in sorted(assignments))
    temporary = path.with_name(f".{path.name}.port-v2-{os.getpid()}")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        stat.st_mode & 0o777,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write("\n".join(retained) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chown(temporary, stat.st_uid, stat.st_gid)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def prepare_deadman_state(
    *,
    admission: PublishedPortApplyAdmissionV2,
    execution: PublishedPortExecutionPlanV2,
    env_file: Path,
    docker_bin: Path,
    deploy_dir: Path,
    compose_files: tuple[Path, ...],
    deadline: datetime,
    now: datetime,
) -> PublishedPortDeadmanStateV2:
    if admission.target_service != execution.plan.intent.service:
        _refuse("deadman target differs from the admitted service")
    if admission.execution_plan_digest != execution.canonical_digest():
        _refuse("deadman state does not bind the admitted plan")
    rows = _env_rows(env_file)
    preimage = []
    for key in sorted(execution.plan.intent.assignments):
        present, value = _env_value(rows, key)
        preimage.append(
            PublishedPortEnvPreimageV2(key=key, present=present, value=value)
        )
    if deadline <= now or deadline - now > timedelta(minutes=10):
        _refuse("deadman deadline must be within the next ten minutes")
    return PublishedPortDeadmanStateV2(
        operation_id=admission.operation_id,
        execution_plan_digest=execution.canonical_digest(),
        service=execution.plan.intent.service,
        deploy_dir=str(deploy_dir.resolve()),
        env_file=str(env_file.resolve()),
        docker_bin=str(docker_bin.resolve()),
        compose_files=tuple(str(path.resolve()) for path in compose_files),
        image_reference=execution.target_image_reference,
        before_image_id=execution.target_image_id,
        env_preimage=tuple(preimage),
        before_container_id=execution.plan.prestate.target_container_id,
        before_listeners=execution.plan.prestate.listeners,
        deadline=deadline,
        updated_at=now,
    )


def verify_effective_ports(
    execution: PublishedPortExecutionPlanV2, effective_compose: Path
) -> None:
    document = _read_json(effective_compose)
    if not isinstance(document, dict) or not isinstance(document.get("services"), dict):
        _refuse("effective Compose document has no services object")
    definition = document["services"].get(execution.plan.intent.service)
    if not isinstance(definition, dict):
        _refuse("effective Compose has no admitted target service")
    actual: set[tuple[int, int, str, str]] = set()
    for row in definition.get("ports") or ():
        if not isinstance(row, dict):
            _refuse("effective Compose port is not normalized long form")
        actual.add(
            (
                int(row["published"]),
                int(row["target"]),
                str(row.get("protocol") or "tcp"),
                str(row.get("host_ip") or ""),
            )
        )
    expected = {
        (
            target.host_port,
            target.container_port,
            target.protocol,
            str(target.bind),
        )
        for target in execution.plan.intent.targets
    }
    if actual != expected:
        _refuse("effective Compose ports differ from the admitted target projection")
    non_port_digest, image = _effective_service_projection(
        effective_compose, execution.plan.intent.service
    )
    if non_port_digest != execution.plan.prestate.non_port_definition_digest:
        _refuse("effective Compose non-port definition drifted before mutation")
    if image != execution.target_image_reference:
        _refuse("effective Compose does not retain the admitted immutable image")


def _proof_map(
    paths: list[Path], contract: type[ContractT]
) -> dict[tuple[str, str], tuple[ContractT, str]]:
    found: dict[tuple[str, str], tuple[ContractT, str]] = {}
    for path in paths:
        proof = _read_contract(path, contract)
        key = (str(proof.target_key), str(proof.client_network))
        if key in found:
            _refuse(f"duplicate proof for {key[0]} from {key[1]}")
        found[key] = (proof, proof.canonical_digest())
    return found


def _address_family_proofs(
    execution: PublishedPortExecutionPlanV2,
    target_after: PublishedPortContainerObservationV2,
) -> tuple[AddressFamilyProofV2, AddressFamilyProofV2]:
    expected = tuple(
        address
        for target in execution.plan.intent.targets
        for address in target.expected_listeners
    )
    observed = tuple(listener.host_ip for listener in target_after.listeners)
    proofs: list[AddressFamilyProofV2] = []
    for version, family in ((4, "ipv4"), (6, "ipv6")):
        wanted = tuple(
            sorted((item for item in expected if item.version == version), key=str)
        )
        got = tuple(
            sorted((item for item in observed if item.version == version), key=str)
        )
        proofs.append(
            AddressFamilyProofV2(
                family=family,
                expected=wanted,
                observed=got,
                matched=True,
            )
        )
    return proofs[0], proofs[1]


def verify_postconditions(
    *,
    execution: PublishedPortExecutionPlanV2,
    admission: PublishedPortApplyAdmissionV2,
    effective_compose: Path,
    containers: Path,
    firewall_paths: list[Path],
    reach_paths: list[Path],
    now: datetime,
) -> PublishedPortPostconditionVerdictV2:
    rows = _normalise_container_rows(containers)
    target_rows = tuple(
        row for row in rows if row.service == execution.plan.intent.service
    )
    if len(target_rows) != 1:
        _refuse("poststate must contain exactly one target container")
    target = target_rows[0]
    before = execution.plan.prestate
    if target.container_id == before.target_container_id:
        _refuse("target container ID did not change")
    if target.image_id != execution.target_image_id:
        _refuse("target container did not reuse the exact running image ID")
    if target.image_reference != execution.target_image_reference:
        _refuse("target container did not reuse the immutable image reference")
    before_others = {
        (row.service, row.container): row.container_id
        for row in before.project_containers
        if row.service != execution.plan.intent.service
    }
    after_others = {
        (row.service, row.container): row.container_id
        for row in rows
        if row.service != execution.plan.intent.service
    }
    if after_others != before_others:
        _refuse("one or more non-target container identities changed")
    non_port_digest, effective_image = _effective_service_projection(
        effective_compose, execution.plan.intent.service
    )
    if non_port_digest != before.non_port_definition_digest:
        _refuse("target non-port service definition changed")
    if effective_image != execution.target_image_reference:
        _refuse("poststate Compose image differs from the admitted immutable image")
    address_families = _address_family_proofs(execution, target)

    firewall = _proof_map(firewall_paths, PublishedPortFirewallProofV2)
    reach = _proof_map(reach_paths, PublishedPortClientReachProofV2)
    obligations = {
        (item.target_key, str(item.client_network))
        for item in execution.client_obligations
    }
    if set(firewall) != obligations or set(reach) != obligations:
        _refuse("firewall and client-reach proofs must exactly cover every obligation")
    for collection in (firewall, reach):
        for proof, _digest in collection.values():
            if proof.operation_id != admission.operation_id:
                _refuse("postcondition proof names another operation")
            if not hmac.compare_digest(
                proof.execution_plan_digest, execution.canonical_digest()
            ):
                _refuse("postcondition proof names another plan")
            if proof.observed_at < admission.admitted_at - MAX_PROOF_SKEW:
                _refuse("postcondition proof predates the admitted operation")
            if proof.observed_at > now + MAX_PROOF_SKEW:
                _refuse("postcondition proof is from the future")
            if isinstance(proof, PublishedPortFirewallProofV2):
                if proof.verifier_identity != admission.firewall_verifier_identity:
                    _refuse("firewall proof comes from an unadmitted verifier")
            elif proof.collector_identity != admission.client_collector_identity:
                _refuse("client reach proof comes from an unadmitted collector")

    other_digest = _sha256_bytes(
        _canonical_json(
            [
                {"service": key[0], "container": key[1], "container_id": value}
                for key, value in sorted(after_others.items())
            ]
        )
    )
    return PublishedPortPostconditionVerdictV2(
        operation_id=admission.operation_id,
        source_sha=admission.source_sha,
        execution_plan_digest=execution.canonical_digest(),
        apply_run_id=admission.apply_run_id,
        target_service=execution.plan.intent.service,
        before_target_container_id=before.target_container_id,
        after_target_container_id=target.container_id,
        target_image_id=target.image_id,
        unchanged_non_target_container_digest=other_digest,
        non_port_definition_digest=non_port_digest,
        address_families=address_families,
        firewall_proof_digests=tuple(
            digest
            for _proof, digest in sorted(firewall.values(), key=lambda item: item[1])
        ),
        client_reach_proof_digests=tuple(
            digest
            for _proof, digest in sorted(reach.values(), key=lambda item: item[1])
        ),
        verified_at=now,
    )


def finalize_outcome(
    *,
    verdict_path: Path,
    deadman_state_path: Path,
    disarmed_at: datetime,
) -> PublishedPortApplyOutcomeV2:
    verdict = _read_contract(verdict_path, PublishedPortPostconditionVerdictV2)
    state = _read_contract(deadman_state_path, PublishedPortDeadmanStateV2)
    if state.operation_id != verdict.operation_id:
        _refuse("deadman state and postcondition verdict name different operations")
    if state.state != "disarmed" or state.state_reason != "verified-success":
        _refuse("deadman state is not a verified-success disarm")
    if state.execution_plan_digest != verdict.execution_plan_digest:
        _refuse("deadman state and verdict bind different plans")
    return PublishedPortApplyOutcomeV2(
        postconditions=verdict,
        deadman_state_digest=state.canonical_digest(),
        deadman_disarmed_at=disarmed_at,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build-plan")
    build.add_argument("--service", required=True)
    build.add_argument("--source-sha", required=True)
    build.add_argument("--target-server-name", required=True)
    build.add_argument("--change-reference", required=True)
    build.add_argument("--reason", required=True)
    build.add_argument("--run-id", required=True, type=int)
    build.add_argument("--planned-at", required=True)
    build.add_argument("--snapshot", required=True, type=Path)
    build.add_argument("--output-dir", required=True, type=Path)
    build.add_argument("--declaration", type=Path, default=DECLARATION)
    build.add_argument("--compose", type=Path, default=COMPOSE)

    replan = commands.add_parser("build-immediate-plan")
    replan.add_argument("--basis-plan", required=True, type=Path)
    replan.add_argument("--effective-compose", required=True, type=Path)
    replan.add_argument("--containers", required=True, type=Path)
    replan.add_argument("--output", required=True, type=Path)
    replan.add_argument("--declaration", type=Path, default=DECLARATION)
    replan.add_argument("--compose", type=Path, default=COMPOSE)

    image_pin = commands.add_parser("write-image-pin")
    image_pin.add_argument("--plan", required=True, type=Path)
    image_pin.add_argument("--output", required=True, type=Path)

    admit = commands.add_parser("admit-apply")
    admit.add_argument("--first-dir", required=True, type=Path)
    admit.add_argument("--second-dir", required=True, type=Path)
    admit.add_argument("--first-observation", required=True, type=Path)
    admit.add_argument("--second-observation", required=True, type=Path)
    admit.add_argument("--source-sha", required=True)
    admit.add_argument("--apply-run-id", required=True, type=int)
    admit.add_argument("--admitted-at", required=True)
    admit.add_argument("--expected-plan-digest", required=True)
    admit.add_argument("--firewall-verifier-identity", required=True)
    admit.add_argument("--client-collector-identity", required=True)
    admit.add_argument("--output", required=True, type=Path)

    verify = commands.add_parser("verify-admission")
    verify.add_argument("--admission", required=True, type=Path)
    verify.add_argument("--plan", required=True, type=Path)
    verify.add_argument("--source-sha", required=True)
    verify.add_argument("--apply-run-id", required=True, type=int)
    verify.add_argument("--now", required=True)

    third = commands.add_parser("verify-third-plan")
    third.add_argument("--admission", required=True, type=Path)
    third.add_argument("--admitted-plan", required=True, type=Path)
    third.add_argument("--immediate-plan", required=True, type=Path)
    third.add_argument("--now", required=True)

    deadman = commands.add_parser("prepare-deadman")
    deadman.add_argument("--admission", required=True, type=Path)
    deadman.add_argument("--plan", required=True, type=Path)
    deadman.add_argument("--env-file", required=True, type=Path)
    deadman.add_argument("--docker-bin", required=True, type=Path)
    deadman.add_argument("--deploy-dir", required=True, type=Path)
    deadman.add_argument("--compose-file", action="append", required=True, type=Path)
    deadman.add_argument("--deadline", required=True)
    deadman.add_argument("--now", required=True)
    deadman.add_argument("--output", required=True, type=Path)

    apply_env = commands.add_parser("apply-env")
    apply_env.add_argument("--admission", required=True, type=Path)
    apply_env.add_argument("--plan", required=True, type=Path)
    apply_env.add_argument("--env-file", required=True, type=Path)
    apply_env.add_argument("--source-sha", required=True)
    apply_env.add_argument("--apply-run-id", required=True, type=int)
    apply_env.add_argument("--now", required=True)

    effective = commands.add_parser("verify-effective")
    effective.add_argument("--plan", required=True, type=Path)
    effective.add_argument("--effective-compose", required=True, type=Path)

    finish = commands.add_parser("verify-postconditions")
    finish.add_argument("--admission", required=True, type=Path)
    finish.add_argument("--plan", required=True, type=Path)
    finish.add_argument("--effective-compose", required=True, type=Path)
    finish.add_argument("--containers", required=True, type=Path)
    finish.add_argument("--firewall-proof", action="append", type=Path, default=[])
    finish.add_argument("--reach-proof", action="append", type=Path, default=[])
    finish.add_argument("--now", required=True)
    finish.add_argument("--output", required=True, type=Path)

    finalize = commands.add_parser("finalize-outcome")
    finalize.add_argument("--verdict", required=True, type=Path)
    finalize.add_argument("--deadman-state", required=True, type=Path)
    finalize.add_argument("--disarmed-at", required=True)
    finalize.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "build-plan":
            execution = build_execution_plan_from_snapshot(
                service=args.service,
                source_sha=args.source_sha,
                target_server_name=args.target_server_name,
                change_reference=args.change_reference,
                reason=args.reason,
                snapshot_path=args.snapshot,
                declaration_path=args.declaration,
                compose_path=args.compose,
            )
            write_plan_artifacts(
                execution_plan=execution,
                run_id=args.run_id,
                planned_at=_utc(args.planned_at),
                output_dir=args.output_dir,
            )
        elif args.command == "build-immediate-plan":
            immediate = build_immediate_plan(
                basis=_read_contract(args.basis_plan, PublishedPortExecutionPlanV2),
                effective_compose=args.effective_compose,
                containers=args.containers,
                declaration_path=args.declaration,
                compose_path=args.compose,
            )
            _write_contract(args.output, immediate)
        elif args.command == "write-image-pin":
            write_image_pin(
                _read_contract(args.plan, PublishedPortExecutionPlanV2),
                args.output,
            )
        elif args.command == "admit-apply":
            admission = admit_apply(
                first_dir=args.first_dir,
                second_dir=args.second_dir,
                first_observation_path=args.first_observation,
                second_observation_path=args.second_observation,
                source_sha=args.source_sha,
                apply_run_id=args.apply_run_id,
                admitted_at=_utc(args.admitted_at),
                expected_plan_digest=args.expected_plan_digest,
                firewall_verifier_identity=args.firewall_verifier_identity,
                client_collector_identity=args.client_collector_identity,
            )
            _write_contract(args.output, admission)
        elif args.command == "verify-admission":
            verify_admission(
                admission_path=args.admission,
                plan_path=args.plan,
                expected_source_sha=args.source_sha,
                expected_apply_run_id=args.apply_run_id,
                now=_utc(args.now),
            )
        elif args.command == "verify-third-plan":
            verify_third_plan(
                args.admitted_plan,
                args.immediate_plan,
                args.admission,
                now=_utc(args.now),
            )
        elif args.command == "prepare-deadman":
            admission = _read_contract(args.admission, PublishedPortApplyAdmissionV2)
            execution = _read_contract(args.plan, PublishedPortExecutionPlanV2)
            state = prepare_deadman_state(
                admission=admission,
                execution=execution,
                env_file=args.env_file,
                docker_bin=args.docker_bin,
                deploy_dir=args.deploy_dir,
                compose_files=tuple(args.compose_file),
                deadline=_utc(args.deadline),
                now=_utc(args.now),
            )
            _write_contract(args.output, state)
        elif args.command == "apply-env":
            _admission, execution = verify_admission(
                admission_path=args.admission,
                plan_path=args.plan,
                expected_source_sha=args.source_sha,
                expected_apply_run_id=args.apply_run_id,
                now=_utc(args.now),
            )
            _atomic_env_update(
                args.env_file,
                {
                    key: value
                    for key, value in execution.plan.intent.assignments.items()
                },
            )
        elif args.command == "verify-effective":
            verify_effective_ports(
                _read_contract(args.plan, PublishedPortExecutionPlanV2),
                args.effective_compose,
            )
        elif args.command == "verify-postconditions":
            admission = _read_contract(args.admission, PublishedPortApplyAdmissionV2)
            execution = _read_contract(args.plan, PublishedPortExecutionPlanV2)
            verdict = verify_postconditions(
                execution=execution,
                admission=admission,
                effective_compose=args.effective_compose,
                containers=args.containers,
                firewall_paths=args.firewall_proof,
                reach_paths=args.reach_proof,
                now=_utc(args.now),
            )
            _write_contract(args.output, verdict)
        elif args.command == "finalize-outcome":
            _write_contract(
                args.output,
                finalize_outcome(
                    verdict_path=args.verdict,
                    deadman_state_path=args.deadman_state,
                    disarmed_at=_utc(args.disarmed_at),
                ),
            )
        else:  # pragma: no cover - argparse owns the closed vocabulary
            _refuse("unsupported command")
    except (ReconcileV2Error, CanonicalContractError, ValidationError) as error:
        print(f"PUBLISHED PORT V2 REFUSED: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
