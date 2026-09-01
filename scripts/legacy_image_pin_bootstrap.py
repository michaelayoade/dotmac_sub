"""Owner of the structurally single-use legacy image-pin bootstrap.

This module makes every decision and holds every refusal.  The shell adapter
and the two Actions workflows are adapters: they collect files, hold locks and
run Docker, but they never decide whether the bootstrap may proceed.

The bootstrap exists for one reason.  ``postgres-local`` publishes 9001 on both
``0.0.0.0`` and ``[::]``; the v4 side is source-restricted to the replication
standby by a DOCKER-USER rule and the v6 side is governed by nothing, because
its traffic terminates on INPUT rather than traversing that chain.  Correcting
that means recreating the container, and the steady-state reconcile refuses to
recreate a container whose image is a mutable tag.  ``postgres-local`` is
tag-pinned, so the steady state can never take its own first step.

This carries the service across that threshold exactly once: from the legacy
tag to the immutable digest of the bytes ALREADY RUNNING, correcting the
listener in the same recreate.  Afterwards ordinary v2 PLAN/APPLY owns the
service, and this program refuses to run again.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Never, TypeVar

from pydantic import ValidationError

from scripts.legacy_image_pin_contracts import (
    BOOTSTRAP_SERVICE,
    LegacyImagePinBootstrapAdmissionV1,
    LegacyImagePinBootstrapDeadmanStateV1,
    LegacyImagePinBootstrapOperationV1,
    LegacyImagePinBootstrapPlanV1,
    LegacyImagePinBootstrapReceiptV1,
    LegacyImagePinBootstrapSnapshotV1,
    LegacyImagePinFirewallProofV1,
    LegacyImagePinPostconditionVerdictV1,
    LegacyImagePinReachProofV1,
    LegacyImagePinReplicationProbeV1,
    overlay_digest,
    overlay_document,
)
from scripts.published_port_contracts import (
    CanonicalContractError,
    PublishedPortObservedListenerV1,
    PublishedPortProjectContainerV1,
    StrictContract,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DECLARATION = REPO_ROOT / "deploy" / "published_ports.toml"
COMPOSE = REPO_ROOT / "docker-compose.yml"
BOOTSTRAP_OBSERVER = REPO_ROOT / "scripts" / "legacy_image_pin_observer.py"
RECEIPT_PATH = Path("/var/lib/dotmac/legacy-image-pin/receipt.json")
MAX_PLAN_AGE = timedelta(hours=1)
MAX_PROOF_SKEW = timedelta(minutes=5)
REPLICATION_CLIENT = "75.119.157.91/32"

ContractT = TypeVar("ContractT", bound=StrictContract)


class BootstrapRefused(RuntimeError):
    """A typed bootstrap gate refused the operation."""


def _refuse(message: str) -> Never:
    raise BootstrapRefused(message)


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


def _read_contract(path: Path, contract: type[ContractT]) -> ContractT:
    try:
        return contract.from_canonical_bytes(path.read_bytes())
    except (OSError, CanonicalContractError) as error:
        raise BootstrapRefused(f"invalid canonical {path.name}: {error}") from error


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


# ---------------------------------------------------------------------------
# Single use
# ---------------------------------------------------------------------------


def require_single_use(receipt_path: Path = RECEIPT_PATH) -> None:
    """Refuse if this bootstrap has already reached a terminal state.

    "Structurally single-use" means the mechanism cannot run twice, not that a
    comment asks it not to.  This is the durable half of that: a root-owned
    terminal receipt, written under the deployment lock, which is checked
    before any authority is minted and before any mutation.

    A ROLLED-BACK bootstrap refuses a repeat just as a successful one does.
    The rollback deliberately retains the immutable image reference, so the
    durable half of the bootstrap's purpose is already achieved and ordinary
    v2 PLAN/APPLY can retry the listener correction on its own; repeating the
    bootstrap would only buy a second unreviewed recreate.
    """

    if not receipt_path.exists():
        return
    try:
        receipt = _read_contract(receipt_path, LegacyImagePinBootstrapReceiptV1)
    except BootstrapRefused:
        # An unreadable receipt is still a receipt. Refusing on a parse failure
        # is the only safe direction: the alternative is that corrupting the
        # file re-enables the operation.
        _refuse(
            "a legacy image-pin receipt exists but could not be parsed; the "
            "bootstrap is single-use and refuses to run again"
        )
    _refuse(
        "the legacy image-pin bootstrap already reached the terminal state "
        f"{receipt.outcome!r} in operation {receipt.operation_id!r}; it is "
        "single-use and cannot run again"
    )


# ---------------------------------------------------------------------------
# PLAN
# ---------------------------------------------------------------------------


def build_plan(
    *,
    snapshot_path: Path,
    source_sha: str,
    change_reference: str,
    reason: str,
    planned_at: datetime,
    declaration_path: Path = DECLARATION,
    compose_path: Path = COMPOSE,
    observer_path: Path = BOOTSTRAP_OBSERVER,
    receipt_path: Path = RECEIPT_PATH,
) -> LegacyImagePinBootstrapPlanV1:
    require_single_use(receipt_path)
    snapshot = _read_contract(snapshot_path, LegacyImagePinBootstrapSnapshotV1)
    if snapshot.observer_digest != sha256_file(observer_path):
        _refuse("the installed bootstrap observer differs from the protected source")
    return LegacyImagePinBootstrapPlanV1(
        source_sha=source_sha,
        change_reference=change_reference,
        reason=reason,
        declaration_digest=sha256_file(declaration_path),
        observer_digest=snapshot.observer_digest,
        deployed_compose_files=snapshot.deployed_compose_files,
        desired_release_compose_digest=sha256_file(compose_path),
        desired_overlay_digest=overlay_digest(snapshot.desired_image_reference),
        legacy_image_reference=snapshot.legacy_image_reference,
        observed_image_id=snapshot.target_image_id,
        desired_image_reference=snapshot.desired_image_reference,
        resolution=snapshot.resolution,
        target_container_id=snapshot.target_container_id,
        current_listeners=snapshot.listeners,
        desired_listeners=(
            PublishedPortObservedListenerV1(
                container_port=5432,
                host_ip="0.0.0.0",  # noqa: S104 - declared, source-restricted
                host_port=9001,
                protocol="tcp",
            ),
        ),
        replication_client=REPLICATION_CLIENT,
        non_port_definition_digest=snapshot.non_port_definition_digest,
        image_free_definition_digest=snapshot.image_free_definition_digest,
        bind_knob=snapshot.bind_knob,
        non_target_containers=snapshot.non_targets,
        operation=LegacyImagePinBootstrapOperationV1(
            desired_image_reference=snapshot.desired_image_reference,
        ),
        planned_at=planned_at,
        expires_at=planned_at + MAX_PLAN_AGE,
    )


def write_plan_artifacts(
    *, plan: LegacyImagePinBootstrapPlanV1, run_id: int, output_dir: Path
) -> None:
    _write_contract(output_dir / "plan.json", plan)
    receipt = {
        "schema": "LegacyImagePinPlanArtifactReceiptV1",
        "source_sha": plan.source_sha,
        "run_id": run_id,
        "run_attempt": 1,
        "artifact_name": f"legacy-image-pin-plan-v1-{plan.source_sha}",
        "artifact_file": "plan.json",
        "plan_digest": plan.canonical_digest(),
        "operation_digest": plan.operation_digest(),
        "prestate_key": plan.prestate_key(),
        "planned_at": plan.planned_at.isoformat().replace("+00:00", "Z"),
        "expires_at": plan.expires_at.isoformat().replace("+00:00", "Z"),
    }
    path = output_dir / "artifact-receipt.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_canonical_json(receipt))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_artifact_receipt(path: Path) -> dict[str, object]:
    raw = path.read_bytes()
    document = json.loads(raw)
    if not isinstance(document, dict):
        _refuse("plan artifact receipt is not an object")
    if raw != _canonical_json(document):
        _refuse("plan artifact receipt is not canonical")
    if document.get("schema") != "LegacyImagePinPlanArtifactReceiptV1":
        _refuse("unsupported plan artifact receipt schema")
    if document.get("run_attempt") != 1:
        _refuse("a plan artifact from a rerun is not admissible")
    return document


# ---------------------------------------------------------------------------
# ADMISSION
# ---------------------------------------------------------------------------


def admit_bootstrap(
    *,
    first_dir: Path,
    second_dir: Path,
    source_sha: str,
    apply_run_id: int,
    admitted_at: datetime,
    expected_plan_digest: str,
    firewall_verifier_identity: str,
    client_collector_identity: str,
    receipt_path: Path = RECEIPT_PATH,
) -> LegacyImagePinBootstrapAdmissionV1:
    """Two distinct, first-attempt, byte-identical plans, or no authority."""

    require_single_use(receipt_path)
    first = _read_contract(first_dir / "plan.json", LegacyImagePinBootstrapPlanV1)
    second = _read_contract(second_dir / "plan.json", LegacyImagePinBootstrapPlanV1)
    if first.canonical_bytes() != second.canonical_bytes():
        _refuse("the two bootstrap plans are not byte-identical")
    if not hmac.compare_digest(first.canonical_digest(), expected_plan_digest):
        _refuse("the reviewed plan digest differs from the two plan artifacts")
    if first.source_sha != source_sha:
        _refuse("plan source differs from the apply source")

    first_receipt = _read_artifact_receipt(first_dir / "artifact-receipt.json")
    second_receipt = _read_artifact_receipt(second_dir / "artifact-receipt.json")
    run_ids = (int(first_receipt["run_id"]), int(second_receipt["run_id"]))
    if run_ids[0] == run_ids[1]:
        _refuse("the two plan artifacts came from the same run")
    for receipt in (first_receipt, second_receipt):
        if receipt["plan_digest"] != first.canonical_digest():
            _refuse("a plan artifact receipt does not bind its plan")
        if receipt["operation_digest"] != first.operation_digest():
            _refuse("a plan artifact receipt does not bind the operation")
        if receipt["prestate_key"] != first.prestate_key():
            _refuse("a plan artifact receipt does not bind the legacy prestate")
        if receipt["source_sha"] != source_sha:
            _refuse("a plan artifact receipt names another source")
        if admitted_at > _utc(str(receipt["expires_at"])):
            _refuse("a plan artifact expired before admission")

    digests = tuple(
        sorted(
            (
                _sha256_bytes(_canonical_json(first_receipt)),
                _sha256_bytes(_canonical_json(second_receipt)),
            )
        )
    )
    return LegacyImagePinBootstrapAdmissionV1(
        source_sha=source_sha,
        apply_run_id=apply_run_id,
        operation_id=f"imagepin-{BOOTSTRAP_SERVICE}-{apply_run_id}",
        plan_digest=first.canonical_digest(),
        operation_digest=first.operation_digest(),
        prestate_key=first.prestate_key(),
        plan_run_ids=tuple(sorted(run_ids)),
        artifact_receipt_digests=digests,
        firewall_verifier_identity=firewall_verifier_identity,
        client_collector_identity=client_collector_identity,
        admitted_at=admitted_at,
        expires_at=admitted_at + timedelta(minutes=30),
    )


# ---------------------------------------------------------------------------
# APPLY preconditions
# ---------------------------------------------------------------------------


def verify_prestate(
    *,
    admission: LegacyImagePinBootstrapAdmissionV1,
    plan: LegacyImagePinBootstrapPlanV1,
    snapshot_path: Path,
    now: datetime,
    receipt_path: Path = RECEIPT_PATH,
) -> LegacyImagePinBootstrapSnapshotV1:
    """Re-observe under the lock and require the exact admitted prestate."""

    require_single_use(receipt_path)
    if now > admission.expires_at:
        _refuse("the bootstrap admission expired")
    if not hmac.compare_digest(admission.plan_digest, plan.canonical_digest()):
        _refuse("the admission does not bind the exact plan bytes")
    if not hmac.compare_digest(admission.operation_digest, plan.operation_digest()):
        _refuse("the admission does not bind the exact operation")
    snapshot = _read_contract(snapshot_path, LegacyImagePinBootstrapSnapshotV1)
    # The CURRENT input is compared FIRST, and on its own coordinate. It is
    # folded into the prestate key too, so a moved host is refused either way
    # -- but the key's message ("the prestate is not the admitted prestate")
    # cannot tell an operator WHICH coordinate moved, and at 03:00 the
    # difference between "someone staged a release" and "this already ran" is
    # the whole diagnosis.
    if snapshot.deployed_compose_files != plan.deployed_compose_files:
        _refuse(
            "the host's deployed Compose bytes have moved since the plan was "
            "taken; that plan describes a host that no longer exists and cannot "
            "be applied -- take fresh plans"
        )
    live = _prestate_from_snapshot(snapshot)
    if not hmac.compare_digest(live, plan.prestate_key()):
        _refuse(
            "the live prestate is not the admitted legacy prestate; the host "
            "changed, or this bootstrap has already run"
        )
    if snapshot.target_container_id != plan.target_container_id:
        _refuse("the live target container differs from the admitted plan")
    if snapshot.desired_image_reference != plan.desired_image_reference:
        _refuse("the live desired digest differs from the admitted plan")
    if snapshot.resolution.resolved_image_id != plan.observed_image_id:
        _refuse("the desired digest no longer resolves to the running image ID")
    if snapshot.non_targets != plan.non_target_containers:
        _refuse("the live non-target container map differs from the admitted plan")
    if snapshot.listeners != plan.current_listeners:
        _refuse("the live listeners differ from the admitted plan")
    if snapshot.bind_knob != plan.bind_knob:
        _refuse("the live bind-variable proof differs from the admitted plan")
    return snapshot


def _prestate_from_snapshot(snapshot: LegacyImagePinBootstrapSnapshotV1) -> str:
    material = "\n".join(
        (
            "LegacyImagePinPrestateV1",
            snapshot.legacy_image_reference,
            snapshot.target_image_id,
            snapshot.target_container_id,
            snapshot.non_port_definition_digest,
            *(f"{row.path}={row.digest}" for row in snapshot.deployed_compose_files),
        )
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(material).hexdigest()}"


def verify_replication_prestate(
    *,
    admission: LegacyImagePinBootstrapAdmissionV1,
    probe_path: Path,
    now: datetime,
) -> LegacyImagePinReplicationProbeV1:
    """Refuse to recreate while replication is not already streaming."""

    probe = _read_contract(probe_path, LegacyImagePinReplicationProbeV1)
    if probe.operation_id != admission.operation_id:
        _refuse("the replication probe names another operation")
    if probe.phase != "prestate":
        _refuse("a prestate replication probe is required before mutation")
    if probe.observed_at < admission.admitted_at - MAX_PROOF_SKEW:
        _refuse("the replication probe predates the admitted operation")
    if probe.observed_at > now + MAX_PROOF_SKEW:
        _refuse("the replication probe is from the future")
    return probe


def prepare_deadman_state(
    *,
    admission: LegacyImagePinBootstrapAdmissionV1,
    plan: LegacyImagePinBootstrapPlanV1,
    env_file: Path,
    docker_bin: Path,
    deploy_dir: Path,
    compose_files: tuple[Path, ...],
    deadline: datetime,
    now: datetime,
) -> LegacyImagePinBootstrapDeadmanStateV1:
    if not hmac.compare_digest(admission.plan_digest, plan.canonical_digest()):
        _refuse("the deadman state does not bind the admitted plan")
    if deadline <= now or deadline - now > timedelta(minutes=10):
        _refuse("the deadman deadline must be within the next ten minutes")
    rows = env_file.read_text(encoding="utf-8").splitlines()
    matches = [
        line.partition("=")[2] for line in rows if line.startswith("PG_LOCAL_BIND=")
    ]
    if len(matches) > 1:
        _refuse("the environment file contains duplicate PG_LOCAL_BIND rows")
    return LegacyImagePinBootstrapDeadmanStateV1(
        operation_id=admission.operation_id,
        plan_digest=plan.canonical_digest(),
        deploy_dir=str(deploy_dir.resolve()),
        env_file=str(env_file.resolve()),
        docker_bin=str(docker_bin.resolve()),
        compose_files=tuple(str(path.resolve()) for path in compose_files),
        # The rollback target keeps the DIGEST, not the legacy tag: the bytes
        # are identical either way, and reverting the reference would put the
        # service back where ordinary v2 PLAN/APPLY cannot reach it.
        retained_image_reference=plan.desired_image_reference,
        before_image_id=plan.observed_image_id,
        bind_was_present=bool(matches),
        bind_preimage=matches[0] if matches else "",
        before_container_id=plan.target_container_id,
        before_listeners=plan.current_listeners,
        deadline=deadline,
        updated_at=now,
    )


# ---------------------------------------------------------------------------
# APPLY postconditions
# ---------------------------------------------------------------------------


def verify_postconditions(
    *,
    admission: LegacyImagePinBootstrapAdmissionV1,
    plan: LegacyImagePinBootstrapPlanV1,
    poststate_path: Path,
    firewall_proof_path: Path,
    reach_proof_path: Path,
    replication_probe_path: Path,
    now: datetime,
) -> LegacyImagePinPostconditionVerdictV1:
    poststate = _read_json_object(poststate_path)
    after_container_id = str(poststate["target_container_id"])
    after_image_id = str(poststate["image_id"])
    after_reference = str(poststate["effective_image_reference"])
    image_free_digest = str(poststate["image_free_definition_digest"])

    if after_container_id == plan.target_container_id:
        _refuse("the target container ID did not change")
    if after_image_id != plan.observed_image_id:
        _refuse("the recreated target does not run the exact prior image ID")
    if after_reference != plan.desired_image_reference:
        _refuse("the recreated target does not carry the desired immutable digest")
    # The non-port projection INCLUDES the image, so it necessarily changes
    # across this bootstrap: that is the whole point of the operation. What
    # must be unchanged is everything else, which is what the image-free
    # digest measures. Asserting the non-port digest itself were unchanged
    # would be asserting the bootstrap did nothing.
    if image_free_digest != plan.image_free_definition_digest:
        _refuse("the target service definition changed beyond its image reference")

    listeners = tuple(
        PublishedPortObservedListenerV1(
            container_port=int(row["container_port"]),
            host_ip=row["host_ip"],
            host_port=int(row["host_port"]),
            protocol=row["protocol"],
        )
        for row in poststate["listeners"]
    )
    non_targets = tuple(
        PublishedPortProjectContainerV1(
            service=row["service"],
            container=row["container"],
            container_id=row["container_id"],
        )
        for row in poststate["non_targets"]
    )
    if non_targets != plan.non_target_containers:
        _refuse("one or more non-target container identities changed")

    firewall = _read_contract(firewall_proof_path, LegacyImagePinFirewallProofV1)
    reach = _read_contract(reach_proof_path, LegacyImagePinReachProofV1)
    probe = _read_contract(replication_probe_path, LegacyImagePinReplicationProbeV1)
    for proof in (firewall, reach, probe):
        if proof.operation_id != admission.operation_id:
            _refuse("a postcondition proof names another operation")
        if proof.observed_at < admission.admitted_at - MAX_PROOF_SKEW:
            _refuse("a postcondition proof predates the admitted operation")
        if proof.observed_at > now + MAX_PROOF_SKEW:
            _refuse("a postcondition proof is from the future")
    if firewall.verifier_identity != admission.firewall_verifier_identity:
        _refuse("the firewall proof comes from an unadmitted verifier")
    if reach.collector_identity != admission.client_collector_identity:
        _refuse("the reach proof comes from an unadmitted collector")
    for proof_digest in (firewall.plan_digest, reach.plan_digest):
        if not hmac.compare_digest(proof_digest, plan.canonical_digest()):
            _refuse("a postcondition proof names another plan")
    if probe.phase != "poststate":
        _refuse("a poststate replication probe is required")

    return LegacyImagePinPostconditionVerdictV1(
        operation_id=admission.operation_id,
        source_sha=admission.source_sha,
        plan_digest=plan.canonical_digest(),
        apply_run_id=admission.apply_run_id,
        before_target_container_id=plan.target_container_id,
        after_target_container_id=after_container_id,
        image_id=after_image_id,
        effective_image_reference=after_reference,
        observed_listeners=listeners,
        image_free_definition_digest=image_free_digest,
        unchanged_non_target_container_digest=_non_target_digest(non_targets),
        replication_state=probe.state,
        firewall_proof_digest=firewall.canonical_digest(),
        client_reach_proof_digest=reach.canonical_digest(),
        verified_at=now,
    )


def _read_json_object(path: Path) -> dict[str, object]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BootstrapRefused(f"cannot read {path.name}: {error}") from error
    if not isinstance(document, dict):
        _refuse(f"{path.name} is not an object")
    return document


def _non_target_digest(rows: tuple[PublishedPortProjectContainerV1, ...]) -> str:
    return _sha256_bytes(
        _canonical_json(
            [
                {
                    "service": row.service,
                    "container": row.container,
                    "container_id": row.container_id,
                }
                for row in rows
            ]
        )
    )


def write_receipt(
    *,
    outcome: str,
    admission: LegacyImagePinBootstrapAdmissionV1,
    plan: LegacyImagePinBootstrapPlanV1,
    after_container_id: str,
    image_id: str,
    recorded_at: datetime,
    receipt_path: Path = RECEIPT_PATH,
) -> LegacyImagePinBootstrapReceiptV1:
    """Write the terminal record that permanently refuses a second bootstrap."""

    if receipt_path.exists():
        _refuse("a legacy image-pin receipt already exists")
    receipt = LegacyImagePinBootstrapReceiptV1(
        outcome=outcome,
        operation_id=admission.operation_id,
        source_sha=admission.source_sha,
        apply_run_id=admission.apply_run_id,
        plan_digest=plan.canonical_digest(),
        operation_digest=plan.operation_digest(),
        prestate_key=plan.prestate_key(),
        legacy_image_reference=plan.legacy_image_reference,
        retained_image_reference=plan.desired_image_reference,
        before_container_id=plan.target_container_id,
        after_container_id=after_container_id,
        image_id=image_id,
        recorded_at=recorded_at,
    )
    _write_contract(receipt_path, receipt)
    return receipt


def write_image_pin(plan: LegacyImagePinBootstrapPlanV1, path: Path) -> None:
    """Write the DESIRED overlay, and only if it is the one the plan bound."""

    document = overlay_document(plan.desired_image_reference)
    if overlay_digest(plan.desired_image_reference) != plan.desired_overlay_digest:
        _refuse("the overlay bytes differ from the admitted desired overlay")
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build-plan")
    build.add_argument("--snapshot", required=True, type=Path)
    build.add_argument("--source-sha", required=True)
    build.add_argument("--change-reference", required=True)
    build.add_argument("--reason", required=True)
    build.add_argument("--planned-at", required=True)
    build.add_argument("--run-id", required=True, type=int)
    build.add_argument("--output-dir", required=True, type=Path)

    admit = commands.add_parser("admit")
    admit.add_argument("--first-dir", required=True, type=Path)
    admit.add_argument("--second-dir", required=True, type=Path)
    admit.add_argument("--source-sha", required=True)
    admit.add_argument("--apply-run-id", required=True, type=int)
    admit.add_argument("--admitted-at", required=True)
    admit.add_argument("--expected-plan-digest", required=True)
    admit.add_argument("--firewall-verifier-identity", required=True)
    admit.add_argument("--client-collector-identity", required=True)
    admit.add_argument("--output", required=True, type=Path)

    prestate = commands.add_parser("verify-prestate")
    prestate.add_argument("--admission", required=True, type=Path)
    prestate.add_argument("--plan", required=True, type=Path)
    prestate.add_argument("--snapshot", required=True, type=Path)
    prestate.add_argument("--replication-probe", required=True, type=Path)
    prestate.add_argument("--now", required=True)

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

    pin = commands.add_parser("write-image-pin")
    pin.add_argument("--plan", required=True, type=Path)
    pin.add_argument("--output", required=True, type=Path)

    verify = commands.add_parser("verify-postconditions")
    verify.add_argument("--admission", required=True, type=Path)
    verify.add_argument("--plan", required=True, type=Path)
    verify.add_argument("--poststate", required=True, type=Path)
    verify.add_argument("--firewall-proof", required=True, type=Path)
    verify.add_argument("--reach-proof", required=True, type=Path)
    verify.add_argument("--replication-probe", required=True, type=Path)
    verify.add_argument("--now", required=True)
    verify.add_argument("--output", required=True, type=Path)

    receipt = commands.add_parser("write-receipt")
    receipt.add_argument("--admission", required=True, type=Path)
    receipt.add_argument("--plan", required=True, type=Path)
    receipt.add_argument("--outcome", required=True, choices=["applied", "rolled_back"])
    receipt.add_argument("--after-container-id", required=True)
    receipt.add_argument("--image-id", required=True)
    receipt.add_argument("--recorded-at", required=True)
    receipt.add_argument("--output", type=Path, default=RECEIPT_PATH)

    guard = commands.add_parser("require-single-use")
    guard.add_argument("--receipt", type=Path, default=RECEIPT_PATH)
    return parser


def main(argv: list[str] | None = None) -> int:  # noqa: C901
    args = _parser().parse_args(argv)
    try:
        if args.command == "build-plan":
            plan = build_plan(
                snapshot_path=args.snapshot,
                source_sha=args.source_sha,
                change_reference=args.change_reference,
                reason=args.reason,
                planned_at=_utc(args.planned_at),
            )
            write_plan_artifacts(
                plan=plan, run_id=args.run_id, output_dir=args.output_dir
            )
        elif args.command == "admit":
            _write_contract(
                args.output,
                admit_bootstrap(
                    first_dir=args.first_dir,
                    second_dir=args.second_dir,
                    source_sha=args.source_sha,
                    apply_run_id=args.apply_run_id,
                    admitted_at=_utc(args.admitted_at),
                    expected_plan_digest=args.expected_plan_digest,
                    firewall_verifier_identity=args.firewall_verifier_identity,
                    client_collector_identity=args.client_collector_identity,
                ),
            )
        elif args.command == "verify-prestate":
            admission = _read_contract(
                args.admission, LegacyImagePinBootstrapAdmissionV1
            )
            plan = _read_contract(args.plan, LegacyImagePinBootstrapPlanV1)
            now = _utc(args.now)
            verify_prestate(
                admission=admission,
                plan=plan,
                snapshot_path=args.snapshot,
                now=now,
            )
            verify_replication_prestate(
                admission=admission, probe_path=args.replication_probe, now=now
            )
        elif args.command == "prepare-deadman":
            _write_contract(
                args.output,
                prepare_deadman_state(
                    admission=_read_contract(
                        args.admission, LegacyImagePinBootstrapAdmissionV1
                    ),
                    plan=_read_contract(args.plan, LegacyImagePinBootstrapPlanV1),
                    env_file=args.env_file,
                    docker_bin=args.docker_bin,
                    deploy_dir=args.deploy_dir,
                    compose_files=tuple(args.compose_file),
                    deadline=_utc(args.deadline),
                    now=_utc(args.now),
                ),
            )
        elif args.command == "write-image-pin":
            write_image_pin(
                _read_contract(args.plan, LegacyImagePinBootstrapPlanV1), args.output
            )
        elif args.command == "verify-postconditions":
            _write_contract(
                args.output,
                verify_postconditions(
                    admission=_read_contract(
                        args.admission, LegacyImagePinBootstrapAdmissionV1
                    ),
                    plan=_read_contract(args.plan, LegacyImagePinBootstrapPlanV1),
                    poststate_path=args.poststate,
                    firewall_proof_path=args.firewall_proof,
                    reach_proof_path=args.reach_proof,
                    replication_probe_path=args.replication_probe,
                    now=_utc(args.now),
                ),
            )
        elif args.command == "write-receipt":
            write_receipt(
                outcome=args.outcome,
                admission=_read_contract(
                    args.admission, LegacyImagePinBootstrapAdmissionV1
                ),
                plan=_read_contract(args.plan, LegacyImagePinBootstrapPlanV1),
                after_container_id=args.after_container_id,
                image_id=args.image_id,
                recorded_at=_utc(args.recorded_at),
                receipt_path=args.output,
            )
        elif args.command == "require-single-use":
            require_single_use(args.receipt)
        else:  # pragma: no cover - argparse owns the closed vocabulary
            _refuse("unsupported command")
    except (BootstrapRefused, CanonicalContractError, ValidationError) as error:
        print(f"LEGACY IMAGE PIN BOOTSTRAP REFUSED: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
