"""Behavioural proofs for the single-use legacy image-pin bootstrap.

Two families of proof live here.

The first is the target/non-target observation split.  Both halves matter and
only one of them is obvious: it is easy to keep proving that a tagged TARGET is
refused, and easy to silently regress the other direction so that a tagged
NON-TARGET starts blocking an operation that never touches it.  Both are
asserted, separately and by name.

The second is that the bootstrap is structurally single-use -- that the
mechanism cannot run twice, rather than that a comment asks it not to.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import scripts.legacy_image_pin_bootstrap as bootstrap
import scripts.legacy_image_pin_deadman as deadman
import scripts.legacy_image_pin_observer as observer
import scripts.legacy_image_pin_staging as staging
import scripts.published_port_reconcile_v2 as v2
from scripts.legacy_image_pin_contracts import (
    LegacyImagePinBootstrapDeadmanStateV1,
    LegacyImagePinBootstrapPlanV1,
    LegacyImagePinBootstrapSnapshotV1,
    LegacyImagePinStagingJournalV1,
    overlay_digest,
    overlay_document,
)
from scripts.published_port_contracts import (
    PublishedPortObservedListenerV1,
    PublishedPortProjectContainerV1,
)

# The exact coordinates measured on dotmac-sub-prod on 2026-09-01. Using the
# real values keeps the fixtures honest: the digest below really is the
# registry digest of the bytes postgres-local is running.
LEGACY_TAG = "postgis/postgis:16-3.4-alpine"
RUNNING_IMAGE_ID = (
    "sha256:681931a625df344215e9b8998bf34daf146b6a395ceacee4439eb9c85869239f"
)
DESIRED_REFERENCE = (
    f"postgis/postgis@{RUNNING_IMAGE_ID.split(':', 1)[0]}:"
    + (RUNNING_IMAGE_ID.split(":", 1)[1])
)
TARGET_BEFORE = "46c9490482a34303a65dad139914a358207da628c00f049e53e0380a2731613c"
TARGET_AFTER = "b" * 64
SOURCE_SHA = "c" * 40
# The plan requires the DESIRED release bytes to be among the bytes deployed on
# the host -- the staging precondition, made structural. So the fixture's
# deployed set really contains this repository's release Compose digest.
RELEASE_DIGEST = bootstrap.sha256_file(bootstrap.COMPOSE)
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)

# Every one of these is a real non-target that runs on the host under a MUTABLE
# TAG. Before the observation split, each of them on its own was enough to fail
# PLAN for postgres-local.
TAGGED_NON_TARGETS = (
    ("redis-local", "dotmac_redis_local", "redis:7-alpine"),
    ("genieacs-mongodb", "dotmac_sub_genieacs_mongodb", "mongo:4.4"),
    ("nominatim", "dotmac_sub_nominatim", "mediagis/nominatim:4.4"),
    (
        "victoriametrics",
        "dotmac_sub_victoriametrics",
        "victoriametrics/victoria-metrics:v1.96.0",
    ),
    ("vmagent", "dotmac_vmagent", "victoriametrics/vmagent:v1.96.0"),
    ("promtail", "dotmac_sub_promtail", "grafana/promtail:3.0.0"),
    (
        "genieacs",
        "dotmac_sub_genieacs",
        "ghcr.io/michaelayoade/dotmac_sub-genieacs:1.2.13",
    ),
    ("freeradius", "dotmac_sub_freeradius", "freeradius/freeradius-server:3.2.7"),
    ("radius-db", "dotmac_radius_pg_test", LEGACY_TAG),
)


def _non_target_rows() -> list[dict[str, object]]:
    return [
        {
            "compose_project": "dotmac_sub",
            "service": service,
            "container": f"/{container}",
            "container_id": f"{index:064x}",
            "image_id": f"sha256:{index + 100:064x}",
            "image_reference": reference,
            "ports": {},
        }
        for index, (service, container, reference) in enumerate(
            TAGGED_NON_TARGETS, start=1
        )
    ]


def _target_row(
    *, reference: str, container_id: str = TARGET_BEFORE
) -> dict[str, object]:
    return {
        "compose_project": "dotmac_sub",
        "service": "postgres-local",
        "container": "/dotmac_pg_local",
        "container_id": container_id,
        "image_id": RUNNING_IMAGE_ID,
        "image_reference": reference,
        "ports": {
            "5432/tcp": [
                {"HostIp": "0.0.0.0", "HostPort": "9001"},
                {"HostIp": "::", "HostPort": "9001"},
            ]
        },
    }


def _write_containers(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _effective(image: str) -> dict[str, object]:
    return {
        "services": {
            "postgres-local": {
                "image": image,
                "environment": {"POSTGRES_PASSWORD": "not-serialized-by-the-owner"},
                "ports": [
                    {"target": 5432, "published": "9001", "protocol": "tcp"},
                ],
                "restart": "unless-stopped",
            }
        }
    }


# ---------------------------------------------------------------------------
# Proof 1: the split, both directions
# ---------------------------------------------------------------------------


def test_a_tagged_target_is_refused_even_when_every_non_target_is_valid(
    tmp_path: Path,
) -> None:
    """The steady-state rule is not weakened by the split.

    Every non-target here is a perfectly ordinary, valid observation. The only
    thing wrong is the one thing that must be wrong: the TARGET -- the
    container this operation would destroy and recreate -- carries a mutable
    tag, so its bytes could change between the plan and the recreate.
    """

    containers = tmp_path / "containers.jsonl"
    effective = tmp_path / "effective.json"
    _write_containers(
        containers, [_target_row(reference=LEGACY_TAG), *_non_target_rows()]
    )
    effective.write_text(json.dumps(_effective(LEGACY_TAG)), encoding="utf-8")

    with pytest.raises(v2.ReconcileV2Error) as refusal:
        v2.build_execution_plan(
            service="postgres-local",
            source_sha=SOURCE_SHA,
            target_server_name="dotmac-sub-prod",
            change_reference="CHG-SUB-9001-CONTAINMENT-2026-09-01",
            reason="Remove the ungoverned IPv6 publish",
            effective_compose=effective,
            containers=containers,
        )
    assert "image_reference" in str(refusal.value) or "immutable" in str(refusal.value)


def test_tagged_non_targets_do_not_block_a_target_only_operation(
    tmp_path: Path,
) -> None:
    """The half that would silently regress.

    Nine of the containers in this project run under mutable tags and always
    will; none of them is recreated by a postgres-local operation. Before the
    split, any one of them failed PLAN. The plan must build, and the resulting
    prestate must still name every one of them -- because their IDENTITY is
    exactly what the postconditions compare afterwards.
    """

    containers = tmp_path / "containers.jsonl"
    effective = tmp_path / "effective.json"
    _write_containers(
        containers, [_target_row(reference=DESIRED_REFERENCE), *_non_target_rows()]
    )
    effective.write_text(json.dumps(_effective(DESIRED_REFERENCE)), encoding="utf-8")

    execution = v2.build_execution_plan(
        service="postgres-local",
        source_sha=SOURCE_SHA,
        target_server_name="dotmac-sub-prod",
        change_reference="CHG-SUB-9001-CONTAINMENT-2026-09-01",
        reason="Remove the ungoverned IPv6 publish",
        effective_compose=effective,
        containers=containers,
    )

    observed = {row.service for row in execution.plan.prestate.project_containers}
    assert observed == {
        "postgres-local",
        *(name for name, _c, _r in TAGGED_NON_TARGETS),
    }
    assert execution.target_image_reference == DESIRED_REFERENCE
    # A non-target's provenance is not merely ignored -- it is unrepresentable,
    # so it cannot be borrowed as evidence about the target either.
    serialized = execution.canonical_bytes().decode()
    for _service, _container, reference in TAGGED_NON_TARGETS:
        if reference != LEGACY_TAG:
            assert reference not in serialized


def test_the_split_still_compares_every_non_target_identity(tmp_path: Path) -> None:
    """Dropping provenance must not become dropping the check."""

    containers = tmp_path / "containers.jsonl"
    effective = tmp_path / "effective.json"
    _write_containers(
        containers, [_target_row(reference=DESIRED_REFERENCE), *_non_target_rows()]
    )
    effective.write_text(json.dumps(_effective(DESIRED_REFERENCE)), encoding="utf-8")
    execution = v2.build_execution_plan(
        service="postgres-local",
        source_sha=SOURCE_SHA,
        target_server_name="dotmac-sub-prod",
        change_reference="CHG-SUB-9001-CONTAINMENT-2026-09-01",
        reason="Remove the ungoverned IPv6 publish",
        effective_compose=effective,
        containers=containers,
    )

    drifted = _non_target_rows()
    drifted[0]["container_id"] = "f" * 64
    after = tmp_path / "after.jsonl"
    _write_containers(
        after,
        [_target_row(reference=DESIRED_REFERENCE, container_id=TARGET_AFTER), *drifted],
    )
    target, non_targets = v2._normalise_container_rows(after, "postgres-local")
    assert target.container_id == TARGET_AFTER
    before = {
        (row.service, row.container): row.container_id
        for row in execution.plan.prestate.project_containers
        if row.service != "postgres-local"
    }
    now = {(row.service, row.container): row.container_id for row in non_targets}
    assert now != before


# ---------------------------------------------------------------------------
# Bootstrap fixtures
# ---------------------------------------------------------------------------


def _snapshot_document(
    *,
    legacy: str = LEGACY_TAG,
    desired: str = DESIRED_REFERENCE,
    resolved_image_id: str = RUNNING_IMAGE_ID,
    listeners: list[dict[str, object]] | None = None,
    wildcard_host_ip: str = "0.0.0.0",
    control_host_ip: str = "127.0.0.1",
    current_host_ip: str = "0.0.0.0",
) -> dict[str, object]:
    return {
        "schema": "LegacyImagePinBootstrapSnapshotV1",
        "target_server_name": "dotmac-sub-prod",
        "service": "postgres-local",
        "observer_digest": f"sha256:{'1' * 64}",
        "legacy_image_reference": legacy,
        "desired_image_reference": desired,
        "resolution": {
            "schema": "LegacyImagePinLocalResolutionV1",
            "reference": desired,
            "resolved_image_id": resolved_image_id,
            "running_image_id": RUNNING_IMAGE_ID,
            "pulled": False,
        },
        "target_container_id": TARGET_BEFORE,
        "target_image_id": RUNNING_IMAGE_ID,
        "volume_identity_digest": f"sha256:{'7' * 64}",
        "listeners": listeners
        if listeners is not None
        else [
            {
                "container_port": 5432,
                "host_ip": "0.0.0.0",
                "host_port": 9001,
                "protocol": "tcp",
            },
            {
                "container_port": 5432,
                "host_ip": "::",
                "host_port": 9001,
                "protocol": "tcp",
            },
        ],
        "non_port_projection": "DockerComposeServiceProjectionV1",
        "non_port_definition_digest": f"sha256:{'2' * 64}",
        "image_free_definition_digest": f"sha256:{'3' * 64}",
        "effective_image_reference": legacy,
        "deployed_compose_files": [
            {
                "path": "/root/dotmac_sub/docker-compose.override.yml",
                "digest": f"sha256:{'b' * 64}",
            },
            {"path": "/root/dotmac_sub/docker-compose.yml", "digest": RELEASE_DIGEST},
        ],
        "bind_knob": {
            "schema": "LegacyImagePinBindKnobProofV1",
            "env_key": "PG_LOCAL_BIND",
            "wildcard_injection": "0.0.0.0:",
            "wildcard_host_ip": wildcard_host_ip,
            "control_injection": "127.0.0.1:",
            "control_host_ip": control_host_ip,
            "current_host_ip": current_host_ip,
            "host_port": 9001,
            "container_port": 5432,
            "protocol": "tcp",
        },
        "non_targets": sorted(
            (
                {
                    "service": service,
                    "container": container,
                    "container_id": f"{index:064x}",
                }
                for index, (service, container, _reference) in enumerate(
                    TAGGED_NON_TARGETS, start=1
                )
            ),
            key=lambda row: (row["service"], row["container"], row["container_id"]),
        ),
    }


def _write_snapshot(path: Path, document: dict[str, object]) -> Path:
    path.write_bytes(observer._canonical(document))
    return path


@pytest.fixture()
def snapshot_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(bootstrap, "RECEIPT_PATH", tmp_path / "absent-receipt.json")
    return _write_snapshot(tmp_path / "snapshot.json", _snapshot_document())


def _build(snapshot: Path, receipt: Path) -> LegacyImagePinBootstrapPlanV1:
    return bootstrap.build_plan(
        snapshot_path=snapshot,
        source_sha=SOURCE_SHA,
        change_reference="CHG-SUB-9001-CONTAINMENT-2026-09-01",
        reason="Pin the running PostGIS bytes so the listener can be corrected",
        planned_at=NOW,
        observer_path=Path(observer.__file__),
        receipt_path=receipt,
    )


@pytest.fixture()
def plan(tmp_path: Path, snapshot_path: Path) -> LegacyImagePinBootstrapPlanV1:
    document = _snapshot_document()
    document["observer_digest"] = bootstrap.sha256_file(Path(observer.__file__))
    _write_snapshot(snapshot_path, document)
    return _build(snapshot_path, tmp_path / "absent-receipt.json")


# ---------------------------------------------------------------------------
# Proof 2: the digest binds the RUNNING bytes, or the bootstrap stops
# ---------------------------------------------------------------------------


def test_the_plan_binds_the_running_bytes_to_the_desired_digest(
    plan: LegacyImagePinBootstrapPlanV1,
) -> None:
    assert plan.legacy_image_reference == LEGACY_TAG
    assert plan.desired_image_reference == DESIRED_REFERENCE
    assert plan.observed_image_id == RUNNING_IMAGE_ID
    assert plan.resolution.resolved_image_id == RUNNING_IMAGE_ID
    assert plan.resolution.pulled is False
    assert plan.production_login == "root@94.72.107.76"
    assert str(plan.replication_client) == "75.119.157.91/32"
    assert plan.operation.desired_bind == "0.0.0.0:"
    assert plan.operation.recreate_flags == (
        "--no-deps",
        "--no-build",
        "--pull",
        "never",
        "--force-recreate",
    )
    assert len(plan.non_target_containers) == len(TAGGED_NON_TARGETS)


def test_a_digest_that_names_other_bytes_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The STOP condition: never adopt a digest that is not what is running.

    A digest resolving to different bytes is exactly what would happen if the
    reference were taken from the mutable tag after upstream published a newer
    image. Adopting it would smuggle an upgrade into a containment change.
    """

    monkeypatch.setattr(bootstrap, "RECEIPT_PATH", tmp_path / "absent.json")
    document = _snapshot_document(resolved_image_id=f"sha256:{'9' * 64}")
    path = _write_snapshot(tmp_path / "snapshot.json", document)
    with pytest.raises(Exception) as refusal:
        LegacyImagePinBootstrapSnapshotV1.from_canonical_bytes(path.read_bytes())
    assert "running image ID" in str(refusal.value)


def test_a_digest_from_another_repository_is_refused(tmp_path: Path) -> None:
    document = _snapshot_document(
        desired=f"docker.io/library/postgres@{RUNNING_IMAGE_ID}"
    )
    path = _write_snapshot(tmp_path / "snapshot.json", document)
    with pytest.raises(Exception) as refusal:
        LegacyImagePinBootstrapSnapshotV1.from_canonical_bytes(path.read_bytes())
    assert "same repository" in str(refusal.value)


# ---------------------------------------------------------------------------
# Proof 3: the bind knob must be live, and the proof must be falsifiable
# ---------------------------------------------------------------------------


def test_a_dead_bind_variable_is_refused(tmp_path: Path) -> None:
    """Measured on the host: the deployed Compose publishes a BARE ``9001:5432``.

    Against that file, setting PG_LOCAL_BIND does nothing at all, so the
    recreate would faithfully reproduce the dual-family publish and the
    containment defect would survive the window.
    """

    document = _snapshot_document(wildcard_host_ip="127.0.0.1")
    path = _write_snapshot(tmp_path / "snapshot.json", document)
    with pytest.raises(Exception) as refusal:
        LegacyImagePinBootstrapSnapshotV1.from_canonical_bytes(path.read_bytes())
    assert "does not interpolate this variable" in str(refusal.value)


def test_a_hardcoded_wildcard_does_not_pass_as_a_live_knob(tmp_path: Path) -> None:
    """The control injection is what makes the wildcard result mean something."""

    document = _snapshot_document(control_host_ip="0.0.0.0")
    path = _write_snapshot(tmp_path / "snapshot.json", document)
    with pytest.raises(Exception) as refusal:
        LegacyImagePinBootstrapSnapshotV1.from_canonical_bytes(path.read_bytes())
    assert "hardcoded, not variable-driven" in str(refusal.value)


# ---------------------------------------------------------------------------
# Proof 4: two distinct byte-identical plans
# ---------------------------------------------------------------------------


def _artifact(
    directory: Path, plan: LegacyImagePinBootstrapPlanV1, run_id: int
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    bootstrap.write_plan_artifacts(plan=plan, run_id=run_id, output_dir=directory)
    return directory


def _admit(
    tmp_path: Path,
    plan: LegacyImagePinBootstrapPlanV1,
    *,
    receipt: Path,
    second_run: int = 202,
):
    return bootstrap.admit_bootstrap(
        first_dir=_artifact(tmp_path / "first", plan, 101),
        second_dir=_artifact(tmp_path / "second", plan, second_run),
        source_sha=SOURCE_SHA,
        apply_run_id=303,
        admitted_at=NOW + timedelta(minutes=1),
        expected_plan_digest=plan.canonical_digest(),
        firewall_verifier_identity="service:sub-firewall-verifier",
        client_collector_identity="service:sub-external-reach-collector",
        receipt_path=receipt,
    )


def test_two_distinct_first_attempt_byte_identical_plans_are_required(
    tmp_path: Path, plan: LegacyImagePinBootstrapPlanV1
) -> None:
    receipt = tmp_path / "absent-receipt.json"
    admission = _admit(tmp_path, plan, receipt=receipt)
    assert admission.plan_run_ids == (101, 202)
    assert admission.operation_id == "imagepin-postgres-local-303"
    assert admission.prestate_key == plan.prestate_key()

    with pytest.raises(bootstrap.BootstrapRefused) as refusal:
        _admit(tmp_path / "same", plan, receipt=receipt, second_run=101)
    assert "same run" in str(refusal.value)


# ---------------------------------------------------------------------------
# Proof 5: structurally single-use
# ---------------------------------------------------------------------------


def test_a_post_bootstrap_prestate_cannot_produce_another_bootstrap_plan(
    tmp_path: Path,
) -> None:
    """A second bootstrap, attempted against the state the first one leaves.

    After the bootstrap the target carries a DIGEST rather than a tag and
    publishes a single IPv4 listener. Neither shape is representable as a
    bootstrap prestate, so the second attempt cannot even be described, let
    alone authorized -- this is the structural half of "single-use".
    """

    post = _snapshot_document(
        legacy=DESIRED_REFERENCE,
        listeners=[
            {
                "container_port": 5432,
                "host_ip": "0.0.0.0",
                "host_port": 9001,
                "protocol": "tcp",
            }
        ],
    )
    path = _write_snapshot(tmp_path / "post.json", post)
    with pytest.raises(Exception) as refusal:
        LegacyImagePinBootstrapSnapshotV1.from_canonical_bytes(path.read_bytes())
    # The digest reference is not a legacy tag, so the snapshot itself is
    # unconstructable.
    assert "legacy_image_reference" in str(refusal.value)


def test_a_terminal_receipt_permanently_refuses_a_second_bootstrap(
    tmp_path: Path, plan: LegacyImagePinBootstrapPlanV1, snapshot_path: Path
) -> None:
    receipt = tmp_path / "receipt.json"
    admission = _admit(tmp_path, plan, receipt=receipt)
    bootstrap.write_receipt(
        outcome="applied",
        admission=admission,
        plan=plan,
        after_container_id=TARGET_AFTER,
        image_id=RUNNING_IMAGE_ID,
        recorded_at=NOW + timedelta(minutes=5),
        receipt_path=receipt,
    )
    assert receipt.exists()

    # Every lane refuses, not just the last one.
    with pytest.raises(bootstrap.BootstrapRefused, match="single-use"):
        bootstrap.require_single_use(receipt)
    with pytest.raises(bootstrap.BootstrapRefused, match="single-use"):
        _build(snapshot_path, receipt)
    with pytest.raises(bootstrap.BootstrapRefused, match="single-use"):
        _admit(tmp_path / "again", plan, receipt=receipt)
    with pytest.raises(bootstrap.BootstrapRefused):
        bootstrap.write_receipt(
            outcome="applied",
            admission=admission,
            plan=plan,
            after_container_id=TARGET_AFTER,
            image_id=RUNNING_IMAGE_ID,
            recorded_at=NOW + timedelta(minutes=6),
            receipt_path=receipt,
        )


def test_a_forward_recovery_receipt_also_refuses_a_repeat(
    tmp_path: Path, plan: LegacyImagePinBootstrapPlanV1
) -> None:
    receipt = tmp_path / "receipt.json"
    admission = _admit(tmp_path, plan, receipt=receipt)
    bootstrap.write_receipt(
        outcome="recovered_forward",
        admission=admission,
        plan=plan,
        after_container_id=TARGET_AFTER,
        image_id=RUNNING_IMAGE_ID,
        recorded_at=NOW + timedelta(minutes=5),
        receipt_path=receipt,
    )
    with pytest.raises(bootstrap.BootstrapRefused, match="recovered_forward"):
        bootstrap.require_single_use(receipt)


def test_an_unreadable_receipt_still_refuses(tmp_path: Path) -> None:
    """Corrupting the receipt must not re-enable the operation."""

    receipt = tmp_path / "receipt.json"
    receipt.write_text("{not json", encoding="utf-8")
    with pytest.raises(bootstrap.BootstrapRefused, match="could not be parsed"):
        bootstrap.require_single_use(receipt)


# ---------------------------------------------------------------------------
# Proof 6: the rollback retains the immutable reference
# ---------------------------------------------------------------------------


def test_the_deadman_target_is_forward_and_keeps_the_pin(
    tmp_path: Path, plan: LegacyImagePinBootstrapPlanV1
) -> None:
    receipt = tmp_path / "receipt.json"
    admission = _admit(tmp_path, plan, receipt=receipt)
    env_file = tmp_path / ".env"
    env_file.write_text("APP_ENV=production\n", encoding="utf-8")
    state = bootstrap.prepare_deadman_state(
        admission=admission,
        plan=plan,
        env_file=env_file,
        docker_bin=Path("/usr/bin/env"),
        deploy_dir=tmp_path,
        compose_files=(tmp_path / "docker-compose.yml",),
        deadline=NOW + timedelta(minutes=5),
        now=NOW,
    )
    # The pin is retained, and the target is FORWARD: exactly one IPv4
    # listener. There is deliberately no before_listeners field to restore.
    assert state.retained_image_reference == DESIRED_REFERENCE
    assert state.retained_image_reference != plan.legacy_image_reference
    assert state.before_image_id == RUNNING_IMAGE_ID
    assert state.forward_bind == "0.0.0.0:"
    assert state.forward_listeners == plan.desired_listeners
    assert len(state.forward_listeners) == 1
    assert state.forward_listeners[0].host_ip.version == 4
    assert state.volume_identity_digest == plan.volume_identity_digest
    assert not hasattr(state, "before_listeners")


def test_the_prestate_key_changes_once_the_bootstrap_has_run(
    plan: LegacyImagePinBootstrapPlanV1,
) -> None:
    """The admitted prestate is not reachable from a post-bootstrap host."""

    recreated = plan.model_copy(update={"target_container_id": TARGET_AFTER})
    assert recreated.prestate_key() != plan.prestate_key()


# ---------------------------------------------------------------------------
# Proof 7: the two inputs — current is revalidated, desired is pinned
# ---------------------------------------------------------------------------


def test_the_plan_binds_current_host_bytes_and_desired_release_bytes(
    plan: LegacyImagePinBootstrapPlanV1,
) -> None:
    """Two inputs, not one.

    CURRENT is what the host actually has: the deployed Compose bytes and the
    live container identities. DESIRED is what APPLY will use: an immutable
    release Compose digest and a fully determined overlay. The Actions checkout
    is never allowed to stand in for the first, and a moving checkout is never
    allowed to supply the second.
    """

    deployed = {row.digest for row in plan.deployed_compose_files}
    assert plan.desired_release_compose_digest in deployed
    assert plan.desired_release_compose_digest == RELEASE_DIGEST
    assert plan.desired_overlay_digest == overlay_digest(DESIRED_REFERENCE)
    assert overlay_document(DESIRED_REFERENCE) == {
        "services": {"postgres-local": {"image": DESIRED_REFERENCE}}
    }
    paths = [row.path for row in plan.deployed_compose_files]
    assert paths == sorted(paths)
    assert all(path.startswith("/") for path in paths)


def test_a_plan_is_unbuildable_until_the_host_is_staged_to_the_release(
    tmp_path: Path,
) -> None:
    """The PG_LOCAL_BIND precondition, made structural rather than remembered.

    Until the host carries the exact release bytes, the desired release digest
    is not among the deployed digests and the plan cannot be constructed at
    all. "Verify the deployed Compose digest" therefore stops being a step
    somebody performs and becomes a thing the contract will not let you skip.
    """

    document = _snapshot_document()
    document["deployed_compose_files"] = [
        {
            "path": "/root/dotmac_sub/docker-compose.override.yml",
            "digest": f"sha256:{'b' * 64}",
        },
        # An UNSTAGED host: still the older release, so the desired bytes are
        # nowhere in the deployed set.
        {"path": "/root/dotmac_sub/docker-compose.yml", "digest": f"sha256:{'e' * 64}"},
    ]
    document["observer_digest"] = bootstrap.sha256_file(Path(observer.__file__))
    path = _write_snapshot(tmp_path / "unstaged.json", document)
    with pytest.raises(Exception, match="stage the release first"):
        _build(path, tmp_path / "absent-receipt.json")


def test_a_plan_whose_deployed_bytes_moved_is_refused_at_apply(
    tmp_path: Path, plan: LegacyImagePinBootstrapPlanV1, snapshot_path: Path
) -> None:
    """No plan taken before staging survives staging.

    Staging the release that carries the knob rewrites the host's deployed
    Compose. A plan taken before that describes a host which no longer exists,
    so APPLY refuses it outright rather than warning — the same shape as the
    single-use refusal, applied to a different coordinate.
    """

    receipt = tmp_path / "absent-receipt.json"
    admission = _admit(tmp_path, plan, receipt=receipt)

    moved = _snapshot_document()
    moved["observer_digest"] = bootstrap.sha256_file(Path(observer.__file__))
    moved["deployed_compose_files"] = [
        {
            "path": "/root/dotmac_sub/docker-compose.override.yml",
            "digest": f"sha256:{'b' * 64}",
        },
        # A third file appeared on the host between planning and apply. It sits
        # in SORTED position deliberately: an unsorted list is refused by the
        # canonical snapshot contract one layer earlier, which would make this
        # test pass for the wrong reason and never exercise the comparison it
        # exists to prove.
        {
            "path": "/root/dotmac_sub/docker-compose.staged.yml",
            "digest": RELEASE_DIGEST,
        },
        {"path": "/root/dotmac_sub/docker-compose.yml", "digest": RELEASE_DIGEST},
    ]
    moved_path = _write_snapshot(tmp_path / "moved.json", moved)

    with pytest.raises(bootstrap.BootstrapRefused, match="no longer exists"):
        bootstrap.verify_prestate(
            admission=admission,
            plan=plan,
            snapshot_path=moved_path,
            now=NOW + timedelta(minutes=2),
            receipt_path=receipt,
        )

    # The same host, unmoved, is still admitted — so the refusal above is
    # discriminating rather than unconditional.
    bootstrap.verify_prestate(
        admission=admission,
        plan=plan,
        snapshot_path=snapshot_path,
        now=NOW + timedelta(minutes=2),
        receipt_path=receipt,
    )


def test_the_prestate_key_covers_the_deployed_bytes(
    plan: LegacyImagePinBootstrapPlanV1,
) -> None:
    """The admission's single prestate coordinate moves when the host does."""

    restaged = plan.model_copy(
        update={
            "deployed_compose_files": (
                *plan.deployed_compose_files[:-1],
                plan.deployed_compose_files[-1].model_copy(
                    update={"digest": f"sha256:{'f' * 64}"}
                ),
            )
        }
    )
    assert restaged.prestate_key() != plan.prestate_key()


def test_a_staged_host_that_would_strand_the_standby_is_refused(
    tmp_path: Path,
) -> None:
    """The hazard staging itself creates, refused before any window is named.

    The release publishes ``${PG_LOCAL_BIND:-127.0.0.1:}9001:5432`` and
    PG_LOCAL_BIND is absent from the production .env. So the instant the
    release Compose is staged, the deployed file resolves to LOOPBACK -- and
    the next recreate, whether this operation's or anyone else's, cuts the
    replication standby off a port it is actively streaming WAL through.

    Staging must set the variable. A plan cannot be built against a host where
    it has not been, which is what stops the hazard being discovered at 03:00.
    """

    document = _snapshot_document(current_host_ip="127.0.0.1")
    path = _write_snapshot(tmp_path / "stranded.json", document)
    with pytest.raises(Exception, match="does not admit the replication"):
        LegacyImagePinBootstrapSnapshotV1.from_canonical_bytes(path.read_bytes())


def test_a_correctly_staged_host_is_still_admitted(tmp_path: Path) -> None:
    """Sensitivity: the refusal above discriminates, it does not refuse all."""

    document = _snapshot_document(current_host_ip="0.0.0.0")
    path = _write_snapshot(tmp_path / "staged.json", document)
    snapshot = LegacyImagePinBootstrapSnapshotV1.from_canonical_bytes(path.read_bytes())
    assert str(snapshot.bind_knob.current_host_ip) == "0.0.0.0"


# ---------------------------------------------------------------------------
# Proof 8: the inverted assertion — dual-family is a FAILURE, not a target
# ---------------------------------------------------------------------------


def test_a_dual_family_forward_target_is_refused(tmp_path: Path) -> None:
    """The inversion, stated as a contract.

    An earlier draft had recovery restore the observed dual-family listeners.
    That was wrong in the most dangerous direction: the IPv6 half of that
    publish terminates on INPUT rather than traversing DOCKER-USER, so no host
    firewall rule reaches it. It is the vulnerability, not a healthy state, and
    a deadman that recreates it has failed rather than recovered.
    """

    dual = (
        PublishedPortObservedListenerV1(
            container_port=5432, host_ip="0.0.0.0", host_port=9001, protocol="tcp"
        ),
        PublishedPortObservedListenerV1(
            container_port=5432, host_ip="::", host_port=9001, protocol="tcp"
        ),
    )
    with pytest.raises(Exception, match="not a recovery state"):
        LegacyImagePinBootstrapDeadmanStateV1(
            operation_id="imagepin-postgres-local-303",
            plan_digest=f"sha256:{'1' * 64}",
            deploy_dir="/root/dotmac_sub",
            env_file="/root/dotmac_sub/.env",
            docker_bin="/usr/bin/docker",
            compose_files=("/var/lib/dotmac/legacy-image-pin/compose-0.yml",),
            retained_image_reference=DESIRED_REFERENCE,
            before_image_id=RUNNING_IMAGE_ID,
            forward_listeners=dual,
            volume_identity_digest=f"sha256:{'7' * 64}",
            before_container_id=TARGET_BEFORE,
            deadline=NOW + timedelta(minutes=5),
            updated_at=NOW,
        )


def test_the_single_ipv4_forward_target_is_accepted(tmp_path: Path) -> None:
    """Sensitivity: the refusal above discriminates rather than refusing all."""

    state = LegacyImagePinBootstrapDeadmanStateV1(
        operation_id="imagepin-postgres-local-303",
        plan_digest=f"sha256:{'1' * 64}",
        deploy_dir="/root/dotmac_sub",
        env_file="/root/dotmac_sub/.env",
        docker_bin="/usr/bin/docker",
        compose_files=("/var/lib/dotmac/legacy-image-pin/compose-0.yml",),
        retained_image_reference=DESIRED_REFERENCE,
        before_image_id=RUNNING_IMAGE_ID,
        forward_listeners=(
            PublishedPortObservedListenerV1(
                container_port=5432, host_ip="0.0.0.0", host_port=9001, protocol="tcp"
            ),
        ),
        volume_identity_digest=f"sha256:{'7' * 64}",
        before_container_id=TARGET_BEFORE,
        deadline=NOW + timedelta(minutes=5),
        updated_at=NOW,
    )
    assert state.state == "armed"
    assert state.forward_bind == "0.0.0.0:"


def test_the_deadman_executor_never_restores_a_preimage() -> None:
    """The executor's own vocabulary must not offer a way backwards."""

    source = Path(deadman.__file__).read_text(encoding="utf-8")
    for gone in ("_restore_bind", "before_listeners", "rollback-now", "rolled_back"):
        assert gone not in source, gone
    assert "_ensure_forward_bind" in source
    assert "may not be recreated automatically" in source
    assert "data/volume identity changed" in source
    assert "_require_database_healthy" in source


# ---------------------------------------------------------------------------
# Proof 9: atomic staging and the commit point
# ---------------------------------------------------------------------------


def test_staging_is_uncommitted_until_the_commit_point(tmp_path: Path) -> None:
    """Before the commit point the host is restorable; after it, it is not.

    The journal state IS the boundary, rather than the boundary being implied
    by wherever an exception happens to be raised.
    """

    common = {
        "source_sha": SOURCE_SHA,
        "compose_path": "/root/dotmac_sub/docker-compose.yml",
        "env_path": "/root/dotmac_sub/.env",
        "observed_compose_digest": f"sha256:{'a' * 64}",
        "observed_env_digest": f"sha256:{'b' * 64}",
        "desired_compose_digest": f"sha256:{'c' * 64}",
        "container_ids_before": (
            PublishedPortProjectContainerV1(
                service="postgres-local",
                container="dotmac_pg_local",
                container_id=TARGET_BEFORE,
            ),
        ),
        "updated_at": NOW,
    }
    preparing = LegacyImagePinStagingJournalV1(state="preparing", **common)
    assert preparing.committed_at is None

    committed = LegacyImagePinStagingJournalV1(
        state="committed", committed_at=NOW + timedelta(seconds=1), **common
    )
    assert committed.committed_at is not None

    # A committed journal with no commit point, or an uncommitted one that
    # claims a commit point, are both incoherent and refused.
    with pytest.raises(Exception, match="names its commit point"):
        LegacyImagePinStagingJournalV1(state="committed", **common)
    with pytest.raises(Exception, match="has no commit point"):
        LegacyImagePinStagingJournalV1(state="preparing", committed_at=NOW, **common)


def test_staging_refuses_when_there_is_nothing_to_stage(tmp_path: Path) -> None:
    digest = f"sha256:{'a' * 64}"
    with pytest.raises(Exception, match="nothing to stage"):
        LegacyImagePinStagingJournalV1(
            source_sha=SOURCE_SHA,
            compose_path="/root/dotmac_sub/docker-compose.yml",
            env_path="/root/dotmac_sub/.env",
            observed_compose_digest=digest,
            observed_env_digest=f"sha256:{'b' * 64}",
            desired_compose_digest=digest,
            container_ids_before=(
                PublishedPortProjectContainerV1(
                    service="postgres-local",
                    container="dotmac_pg_local",
                    container_id=TARGET_BEFORE,
                ),
            ),
            state="preparing",
            updated_at=NOW,
        )


def test_a_torn_write_leaves_a_restorable_host_before_the_commit_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both files land, or neither does.

    The pairing is what matters: a host carrying the release Compose without
    PG_LOCAL_BIND resolves the publish to loopback, and the next recreate
    strands the standby. So a torn write must leave the host exactly as it was
    observed, not half-applied.
    """

    root = tmp_path / "state"
    compose = tmp_path / "docker-compose.yml"
    env_file = tmp_path / ".env"
    release = tmp_path / "release-compose.yml"
    compose.write_text("bare: 9001:5432\n", encoding="utf-8")
    env_file.write_text("APP_ENV=production\n", encoding="utf-8")
    release.write_text(
        "knob: ${PG_LOCAL_BIND:-127.0.0.1:}9001:5432\n", encoding="utf-8"
    )

    monkeypatch.setattr(staging, "STATE_ROOT", root)
    monkeypatch.setattr(staging, "JOURNAL", root / "staging-journal.json")
    monkeypatch.setattr(staging, "PRESERVED", root / "preserved")
    monkeypatch.setattr(
        staging,
        "_container_map",
        lambda _bin: [
            {
                "service": "postgres-local",
                "container": "dotmac_pg_local",
                "container_id": TARGET_BEFORE,
            }
        ],
    )
    observed_compose = compose.read_bytes()
    observed_env = env_file.read_bytes()

    # Tear the operation between the two file landings.
    real_replace = staging._atomic_replace
    calls: list[Path] = []

    def tearing(path: Path, payload: bytes, mode: int, uid: int, gid: int) -> None:
        calls.append(path)
        if path == env_file:
            raise OSError("simulated torn write")
        real_replace(path, payload, mode, uid, gid)

    monkeypatch.setattr(staging, "_atomic_replace", tearing)
    with pytest.raises(OSError, match="simulated torn write"):
        staging.stage(
            compose_path=compose,
            env_path=env_file,
            release=release,
            source_sha=SOURCE_SHA,
            docker_bin="/usr/bin/docker",
        )

    # Half-applied: the Compose moved, the variable did not. This is exactly
    # the state that strands the standby, and it is what recovery must undo.
    assert compose.read_bytes() != observed_compose
    monkeypatch.setattr(staging, "_atomic_replace", real_replace)
    journal = staging._read_journal()
    assert journal["state"] == "preparing"

    staging.recover()
    assert compose.read_bytes() == observed_compose
    assert env_file.read_bytes() == observed_env
    assert not (root / "staging-journal.json").exists()


def test_after_the_commit_point_recovery_refuses_to_go_backwards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regime boundary, observed.

    Past the commit point the pre-staging Compose is deliberately gone: keeping
    a known-vulnerable definition on disk in order to return to it is the
    convenience the ruling refuses. Break-glass is a separate authorization.
    """

    root = tmp_path / "state"
    compose = tmp_path / "docker-compose.yml"
    env_file = tmp_path / ".env"
    release = tmp_path / "release-compose.yml"
    compose.write_text("bare: 9001:5432\n", encoding="utf-8")
    env_file.write_text("APP_ENV=production\n", encoding="utf-8")
    release.write_text(
        "knob: ${PG_LOCAL_BIND:-127.0.0.1:}9001:5432\n", encoding="utf-8"
    )

    monkeypatch.setattr(staging, "STATE_ROOT", root)
    monkeypatch.setattr(staging, "JOURNAL", root / "staging-journal.json")
    monkeypatch.setattr(staging, "PRESERVED", root / "preserved")
    monkeypatch.setattr(
        staging,
        "_container_map",
        lambda _bin: [
            {
                "service": "postgres-local",
                "container": "dotmac_pg_local",
                "container_id": TARGET_BEFORE,
            }
        ],
    )
    staging.stage(
        compose_path=compose,
        env_path=env_file,
        release=release,
        source_sha=SOURCE_SHA,
        docker_bin="/usr/bin/docker",
    )

    # Both landed together, and the bind is set.
    assert compose.read_text(encoding="utf-8") == release.read_text(encoding="utf-8")
    assert "PG_LOCAL_BIND=0.0.0.0:" in env_file.read_text(encoding="utf-8")
    assert staging._read_journal()["state"] == "committed"
    # The way back is destroyed once it must never be taken.
    assert not (root / "preserved" / "compose.observed").exists()

    with pytest.raises(staging.StagingError, match="FORWARD"):
        staging.recover()


def test_staging_proves_it_recreated_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    monkeypatch.setattr(staging, "STATE_ROOT", root)
    monkeypatch.setattr(staging, "JOURNAL", root / "staging-journal.json")
    monkeypatch.setattr(staging, "PRESERVED", root / "preserved")
    before = [
        {
            "service": "postgres-local",
            "container": "dotmac_pg_local",
            "container_id": TARGET_BEFORE,
        }
    ]
    root.mkdir(parents=True)
    staging._write_journal(
        {
            "schema": "LegacyImagePinStagingJournalV1",
            "target_server_name": "dotmac-sub-prod",
            "service": "postgres-local",
            "source_sha": SOURCE_SHA,
            "compose_path": "/root/dotmac_sub/docker-compose.yml",
            "env_path": "/root/dotmac_sub/.env",
            "observed_compose_digest": f"sha256:{'a' * 64}",
            "observed_env_digest": f"sha256:{'b' * 64}",
            "desired_compose_digest": f"sha256:{'c' * 64}",
            "bind_env": "PG_LOCAL_BIND",
            "desired_bind": "0.0.0.0:",
            "container_ids_before": before,
            "state": "committed",
            "committed_at": "2026-09-01T12:00:00Z",
            "updated_at": "2026-09-01T12:00:00Z",
        }
    )
    monkeypatch.setattr(staging, "_container_map", lambda _bin: before)
    staging.confirm_no_recreate("/usr/bin/docker")

    recreated = [dict(before[0], container_id="d" * 64)]
    monkeypatch.setattr(staging, "_container_map", lambda _bin: recreated)
    with pytest.raises(staging.StagingError, match="recreate nothing"):
        staging.confirm_no_recreate("/usr/bin/docker")
