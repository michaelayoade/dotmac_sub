"""Hosted contract checks for the production migration interlock."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from scripts import production_deploy_migration as migration
from scripts import production_deploy_probe as probe
from scripts import production_trusted_rollback_preflight as preflight


def _binding() -> probe.CapturedNetworkBinding:
    database = probe.EndpointBinding(
        "a" * 64,
        "sha256:" + "b" * 64,
        "2026-10-15T00:00:00Z",
        0,
        "c" * 64,
        "172.20.0.2",
        ("postgres-local",),
    )
    broker = probe.EndpointBinding(
        "d" * 64,
        "sha256:" + "e" * 64,
        "2026-10-15T00:00:00Z",
        0,
        "f" * 64,
        "172.20.0.3",
        ("redis-local",),
    )
    return probe.CapturedNetworkBinding(
        "1" * 64,
        "dotmac_default",
        "2" * 64,
        "sha256:" + "3" * 64,
        "sha256:" + "4" * 64,
        "5" * 40,
        "2026-10-15T00:00:00Z",
        0,
        "sha256:" + "6" * 64,
        "sha256:" + "7" * 64,
        "8" * 40,
        "postgres-local",
        database,
        "redis-local",
        broker,
        (),
        (),
        (
            "--project-directory",
            str(Path(__file__).resolve().parents[1]),
            "--env-file",
            str(Path(__file__).resolve().parents[1] / ".env"),
            "-f",
            str(Path(__file__).resolve().parents[1] / "docker-compose.yml"),
        ),
        "",
        "",
        "CELERY_BROKER_URL",
    )


def _record(phase: migration.MigrationPhase) -> migration.MigrationInvocation:
    binding = _binding()
    identity = migration.MigrationIdentity(
        11,
        10,
        "8" * 40,
        "sha256:" + "7" * 64,
        binding.candidate_image_id,
        "1e8e4220-55e5-49c2-b456-1ab889d7cffa",
        1,
        "dotmac_sub_migrate_11_1e8e422055e549c2",
        "9" * 64,
    )
    terminal = None
    detail = None
    if phase in {
        migration.MigrationPhase.EXITED,
        migration.MigrationPhase.REMOVE_REQUESTED,
        migration.MigrationPhase.REMOVED,
        migration.MigrationPhase.COMPLETE,
        migration.MigrationPhase.SAFE_PREDDL,
    }:
        code = 0 if phase is migration.MigrationPhase.COMPLETE else 1
        terminal = migration.MigrationTerminalEvidence(
            "9" * 64,
            binding.candidate_image_id,
            identity.container_name,
            identity.invocation_id,
            "2026-10-15T00:00:00Z",
            "2026-10-15T00:00:01Z",
            code,
            False,
            0,
            ("/opt/venv/bin/python",),
            ("-I", "-B", "-m", "alembic", "upgrade", "heads"),
            binding.network_id,
        )
        detail = "exit-nonzero" if code else None
    return migration.MigrationInvocation(
        identity=identity,
        phase=phase,
        heads_before=("a1",),
        candidate_heads=("b2",),
        network_binding=binding.public_json(),
        terminal=terminal,
        terminal_detail=detail,
    )


def test_missing_initial_journal_is_allowed_but_pending_journal_blocks(
    tmp_path: Path,
) -> None:
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    migration.guard_interlock(deploy)
    path = migration._journal_path(deploy)
    migration._write(path, _record(migration.MigrationPhase.START_REQUESTED))

    with pytest.raises(migration.MigrationRefused, match="migration_interlock_pending"):
        migration.guard_interlock(deploy)


def test_reconciliation_rejects_stale_invocation_binding_before_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    record = _record(migration.MigrationPhase.START_REQUESTED)
    migration._write(migration._journal_path(deploy), record)
    commands: list[list[str]] = []
    monkeypatch.setattr(probe, "_run", lambda command, **_kw: commands.append(command))

    with pytest.raises(
        migration.MigrationRefused, match="migration_reconciliation_stale"
    ):
        migration.reconcile_pending(
            migration.MigrationReconciliationRequest(
                deploy,
                "1e8e4220-55e5-49c2-b456-1ab889d7cffa",
                "0" * 64,
            )
        )

    assert commands == []


def test_reconciliation_classifies_retained_terminal_without_touching_migration_or_writers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    record = _record(migration.MigrationPhase.START_REQUESTED)
    migration._write(migration._journal_path(deploy), record)
    terminal = _record(migration.MigrationPhase.SAFE_PREDDL).terminal
    assert terminal is not None
    value = {"State": {"Status": "exited"}}
    command_calls: list[list[str]] = []
    probe_operations: list[str] = []
    monkeypatch.setattr(
        probe,
        "_verify",
        lambda *_args, **_kwargs: (
            "postgresql://u:p@postgres-local/db",
            "redis://:p@redis-local/10",
            "CELERY_BROKER_URL",
        ),
    )
    monkeypatch.setattr(migration, "_captured_writers_stopped", lambda _binding: True)
    monkeypatch.setattr(migration, "_inspect", lambda _identifier: value)
    monkeypatch.setattr(migration, "_inspect_exact", lambda *_args, **_kwargs: value)
    monkeypatch.setattr(migration, "_terminal", lambda *_args: terminal)
    monkeypatch.setattr(
        migration,
        "_observe_heads",
        lambda _binding, *, image, current: (
            record.heads_before if current else record.candidate_heads
        ),
    )
    monkeypatch.setattr(
        probe,
        "_probe",
        lambda _binding, *, image, module, arguments, operation: (
            probe_operations.append(operation) or ""
        ),
    )
    monkeypatch.setattr(
        probe,
        "_run",
        lambda command, **_kwargs: command_calls.append(command),
    )

    outcome = migration.reconcile_pending(
        migration.MigrationReconciliationRequest(
            deploy,
            record.identity.invocation_id,
            migration._load_journal(migration._journal_path(deploy))[1] or "",
        )
    )

    assert outcome.disposition is migration.MigrationDisposition.SAFE_PREDDL
    assert outcome.phase is migration.MigrationPhase.SAFE_PREDDL
    assert probe_operations == [
        "payment-email-predecessor",
        "payments-predecessor",
    ]
    assert command_calls == []


def test_maintenance_context_reader_refuses_linked_payload_before_lock_check(
    tmp_path: Path,
) -> None:
    path = tmp_path / "maintenance.json"
    path.write_text("{}")
    path.chmod(0o600)
    descriptor = os.open(path, os.O_RDWR)
    try:
        with pytest.raises(
            migration.MigrationRefused, match="maintenance_context_unproved"
        ):
            migration.read_maintenance_context(descriptor)
    finally:
        os.close(descriptor)


def test_maintenance_context_reader_treats_closed_descriptor_as_absent() -> None:
    assert migration.read_maintenance_context(-1) is None


def test_explicit_closed_maintenance_fd_never_falls_back_to_ordinary_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[bool] = []
    monkeypatch.setattr(
        migration,
        "run_migration",
        lambda **_kwargs: called.append(True) or 0,
    )

    result = migration.main(
        [
            "run",
            "--deploy-directory",
            str(tmp_path),
            "--binding-json",
            "{}",
            "--run-id",
            "11",
            "--authorization-run-id",
            "10",
            "--target-revision",
            "a" * 40,
            "--candidate-digest",
            "sha256:" + "b" * 64,
            "--heads-before",
            "a1",
            "--candidate-head",
            "b2",
            "--maintenance-context-fd",
            "-1",
        ]
    )

    assert result == 3
    assert called == []


@pytest.mark.parametrize("wrong_lock_path", [False, True])
def test_unlinked_readonly_maintenance_context_requires_canonical_held_lock(
    wrong_lock_path: bool,
    tmp_path: Path,
) -> None:
    context = migration.MaintenanceExecutionContext(
        deploy_directory=tmp_path.resolve(),
        control_repository=tmp_path.resolve(),
        authorization_main_revision="a" * 40,
        production_authorization_path=(tmp_path / "authorization.json").resolve(),
        authorization_run_id=10,
        target_revision="b" * 40,
        target_digest="sha256:" + "c" * 64,
        target_image_id="sha256:" + "d" * 64,
        failed_invocation_id="1e8e4220-55e5-49c2-b456-1ab889d7cffa",
        failed_journal_sha256="e" * 64,
        deployment_run_id=77,
    )
    context_path = tmp_path / "maintenance.json"
    context_path.write_bytes(migration._canonical(context.public_json()))
    context_path.chmod(0o600)
    context_fd = os.open(context_path, os.O_RDONLY)
    context_path.unlink()
    lock_path = tmp_path / "canonical.lock"
    lock_path.touch(mode=0o600)
    lock_path.chmod(0o600)
    lock_fd = os.open(lock_path, os.O_RDWR)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    expected_lock = tmp_path / "different.lock" if wrong_lock_path else lock_path
    if wrong_lock_path:
        expected_lock.touch(mode=0o600)
        expected_lock.chmod(0o600)
    repository = Path(__file__).resolve().parents[1]
    child = """
import os, sys
import fcntl
from pathlib import Path
sys.path.insert(0, sys.argv[4])
from scripts import production_deploy_migration as migration
migration._CANONICAL_LOCK = Path(sys.argv[1])
context_source = fcntl.fcntl(int(sys.argv[2]), fcntl.F_DUPFD_CLOEXEC, 9)
lock_source = fcntl.fcntl(int(sys.argv[3]), fcntl.F_DUPFD_CLOEXEC, 9)
os.dup2(context_source, 7, inheritable=True)
os.dup2(lock_source, 8, inheritable=True)
os.close(context_source)
os.close(lock_source)
try:
    context = migration.read_maintenance_context()
except migration.MigrationRefused:
    raise SystemExit(42)
raise SystemExit(0 if context is not None and context.authorization_run_id == 10
                 and context.deployment_run_id == 77 else 1)
"""
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                child,
                str(expected_lock),
                str(context_fd),
                str(lock_fd),
                str(repository),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
            pass_fds=(context_fd, lock_fd),
        )
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
        os.close(context_fd)

    assert result.returncode == (42 if wrong_lock_path else 0), result.stderr


def test_maintenance_admission_rejects_context_target_mismatch_before_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = _binding()
    context = migration.MaintenanceExecutionContext(
        deploy_directory=tmp_path.resolve(),
        control_repository=tmp_path.resolve(),
        authorization_main_revision="a" * 40,
        production_authorization_path=(tmp_path / "authorization.json").resolve(),
        authorization_run_id=10,
        target_revision="9" * 40,
        target_digest="sha256:" + "7" * 64,
        target_image_id="sha256:" + "6" * 64,
        failed_invocation_id="1e8e4220-55e5-49c2-b456-1ab889d7cffa",
        failed_journal_sha256="e" * 64,
        deployment_run_id=11,
    )
    commands: list[list[str]] = []
    monkeypatch.setattr(probe, "_run", lambda command: commands.append(command))

    with pytest.raises(
        migration.MigrationRefused, match="maintenance_context_invocation_mismatch"
    ):
        migration.validate_maintenance_admission(context, binding)

    assert commands == []


def test_prepare_maintenance_context_refuses_replayed_journal_digest(
    tmp_path: Path,
) -> None:
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    record = _record(migration.MigrationPhase.COMPLETE)
    migration._write(migration._journal_path(deploy), record)
    context = migration.MaintenanceExecutionContext(
        deploy_directory=deploy.resolve(),
        control_repository=tmp_path.resolve(),
        authorization_main_revision="a" * 40,
        production_authorization_path=(tmp_path / "authorization.json").resolve(),
        authorization_run_id=10,
        target_revision=record.identity.target_revision,
        target_digest=record.identity.candidate_digest,
        target_image_id=record.identity.candidate_image_id,
        failed_invocation_id=record.identity.invocation_id,
        failed_journal_sha256="0" * 64,
        deployment_run_id=11,
    )

    with pytest.raises(migration.MigrationRefused, match="maintenance_journal_stale"):
        migration.prepare_maintenance_context(context)


def _maintenance_admission_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    failure: str | None = None,
) -> tuple[migration.MaintenanceExecutionContext, probe.CapturedNetworkBinding]:
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    record = _record(migration.MigrationPhase.COMPLETE)
    migration._write(migration._journal_path(deploy), record)
    _stored_record, journal_digest = migration._load_journal(
        migration._journal_path(deploy)
    )
    assert journal_digest is not None
    binding = _binding()
    context = migration.MaintenanceExecutionContext(
        deploy_directory=deploy.resolve(),
        control_repository=tmp_path.resolve(),
        authorization_main_revision="a" * 40,
        production_authorization_path=(tmp_path / "authorization.json").resolve(),
        authorization_run_id=10,
        target_revision=record.identity.target_revision,
        target_digest=record.identity.candidate_digest,
        target_image_id=record.identity.candidate_image_id,
        failed_invocation_id=record.identity.invocation_id,
        failed_journal_sha256=journal_digest,
        deployment_run_id=11,
    )
    monkeypatch.setattr(preflight, "_verify_authorization", lambda *_: None)
    monkeypatch.setattr(
        preflight,
        "_classify_transition",
        lambda *_args, **_kwargs: preflight.SourceTransition.SAME,
    )
    monkeypatch.setattr(
        probe,
        "_candidate",
        lambda *_args, **_kwargs: (
            record.identity.candidate_image_id,
            record.identity.candidate_digest,
            {},
        ),
    )
    monkeypatch.setattr(
        probe,
        "_verify",
        lambda *_args, **_kwargs: (
            "postgresql://u:p@postgres-local/db",
            "redis://:p@redis-local/10",
            "CELERY_BROKER_URL",
        ),
    )
    monkeypatch.setattr(
        migration,
        "_inspect",
        lambda *_: (_ for _ in ()).throw(probe.ProbeRefused("absent")),
    )
    monkeypatch.setattr(migration, "_exact_container_absence_proven", lambda _: True)
    monkeypatch.setattr(
        migration,
        "_captured_writers_stopped",
        lambda _: failure != "writers",
    )
    monkeypatch.setattr(
        probe,
        "verify_fixed_compose_inventory",
        lambda *_args, **_kwargs: (
            (_ for _ in ()).throw(probe.ProbeRefused("inventory"))
            if failure == "inventory"
            else None
        ),
    )
    monkeypatch.setattr(
        migration,
        "_observe_heads",
        lambda _binding, *, image, current: (
            ("a1",) if failure == "heads" else record.candidate_heads
        ),
    )

    def fixed_probe(
        _binding: probe.CapturedNetworkBinding,
        *,
        image: str,
        module: str,
        arguments: tuple[str, ...],
        operation: str,
    ) -> str:
        assert image == "candidate"
        if operation == "payments-installed":
            complete = failure != "floor"
            return json.dumps(
                {
                    "schema_version": 1,
                    "floor": "installed",
                    "observation": {
                        "legacy_shape_complete": True,
                        "installed_shape_complete": complete,
                        "correlation_columns": 2 if complete else 0,
                        "correlation_constraints": 2 if complete else 0,
                        "correlation_triggers": 1 if complete else 0,
                        "correlation_functions": 1 if complete else 0,
                    },
                }
            )
        if operation == "drain":
            empty = failure != "broker"
            observation = {
                "mode": "broker-only",
                "queues": [
                    {"queue": f"queue-{index}", "ready": 0 if empty else 1}
                    for index in range(11)
                ],
                "unacknowledged": {"hash_entries": 0, "index_entries": 0},
            }
            return json.dumps(
                {
                    "schema_version": 1,
                    "mode": "broker-only",
                    "outcome": "stable-empty" if empty else "observed-busy",
                    "refusal": None,
                    "observation": observation,
                    "previous_zero": observation if empty else None,
                }
            )
        raise AssertionError("unexpected fixed probe operation")

    monkeypatch.setattr(probe, "_probe", fixed_probe)
    return context, binding


def test_maintenance_forward_admission_accepts_exact_stopped_installed_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context, binding = _maintenance_admission_fixture(tmp_path, monkeypatch)

    admission = migration.validate_maintenance_admission(context, binding)

    assert admission.failed_invocation_id == context.failed_invocation_id
    assert admission.failed_journal_sha256 == context.failed_journal_sha256
    assert admission.target_revision == context.target_revision


def test_superseded_maintenance_context_cannot_start_new_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context, binding = _maintenance_admission_fixture(tmp_path, monkeypatch)
    journal = migration._journal_path(context.deploy_directory)
    prior = _record(migration.MigrationPhase.COMPLETE)
    replacement = replace(
        prior,
        identity=replace(prior.identity, attempt=prior.identity.attempt + 1),
    )
    migration._write(journal, replacement)
    commands: list[list[str]] = []
    monkeypatch.setattr(probe, "_run", lambda command, **_kw: commands.append(command))

    with pytest.raises(
        migration.MigrationRefused, match="maintenance_journal_unproved"
    ):
        migration.run_migration(
            deploy_directory=context.deploy_directory,
            binding_json=json.dumps(binding.public_json()),
            run_id=context.deployment_run_id,
            authorization_run_id=context.authorization_run_id,
            target_revision=context.target_revision,
            candidate_digest=context.target_digest,
            heads_before=("b2",),
            candidate_heads=("b2",),
            budget_seconds=1200,
            maintenance_context=context,
        )

    assert commands == []


@pytest.mark.parametrize(
    ("failure", "refusal"),
    [
        ("heads", "maintenance_heads_unproved"),
        ("floor", "maintenance_admission_unproved"),
        ("writers", "maintenance_writers_not_stopped"),
        ("inventory", "maintenance_admission_unproved"),
        ("broker", "maintenance_broker_empty_unproved"),
    ],
)
def test_maintenance_forward_admission_refuses_unproved_cutover_fact(
    failure: str,
    refusal: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, binding = _maintenance_admission_fixture(
        tmp_path, monkeypatch, failure=failure
    )

    with pytest.raises(migration.MigrationRefused, match=refusal):
        migration.validate_maintenance_admission(context, binding)


def test_unrecognized_journal_shape_refuses_without_interpreting_as_empty(
    tmp_path: Path,
) -> None:
    deploy = tmp_path / "deploy"
    directory = deploy / ".deployment-state"
    directory.mkdir(parents=True, mode=0o700)
    os.chmod(directory, 0o700)
    (directory / "migration.json").write_text(
        json.dumps(
            {
                "schema": "DotmacProductionMigrationJournalV1",
                "invocation": {},
                "skip": True,
            }
        )
    )
    os.chmod(directory / "migration.json", 0o600)

    with pytest.raises(migration.MigrationRefused):
        migration.guard_interlock(deploy)


def test_canonical_journal_with_cross_identity_mismatch_refuses(
    tmp_path: Path,
) -> None:
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    path = migration._journal_path(deploy)
    migration._write(path, _record(migration.MigrationPhase.COMPLETE))
    document = json.loads(path.read_bytes())
    document["invocation"]["identity"]["candidate_image_id"] = "sha256:" + "f" * 64
    path.write_bytes(migration._canonical(document))
    path.chmod(0o600)

    with pytest.raises(migration.MigrationRefused, match="migration_journal_unproved"):
        migration._load_journal(path)


def test_journal_fsync_failure_prevents_migration_docker_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    binding = _binding()
    calls: list[list[str]] = []
    monkeypatch.setattr(
        probe,
        "_verify",
        lambda *_args, **_kwargs: (
            "postgresql://u:p@postgres-local/db",
            "redis://:p@redis-local/10",
            "CELERY_BROKER_URL",
        ),
    )
    monkeypatch.setattr(
        migration,
        "_image_details",
        lambda _binding: {
            "Config": {"Labels": {"org.opencontainers.image.revision": "8" * 40}}
        },
    )
    monkeypatch.setattr(probe, "_run", lambda command, **_kwargs: calls.append(command))
    monkeypatch.setattr(
        migration.os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("disk"))
    )

    with pytest.raises(
        migration.MigrationRefused, match="migration_journal_write_failed"
    ):
        migration.run_migration(
            deploy_directory=deploy,
            binding_json=json.dumps(binding.public_json()),
            run_id=11,
            authorization_run_id=10,
            target_revision="8" * 40,
            candidate_digest="sha256:" + "7" * 64,
            heads_before=("a1",),
            candidate_heads=("b2",),
            budget_seconds=1200,
        )
    assert calls == []


def test_failed_parent_fsync_is_retried_before_later_journal_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    path = migration._journal_path(deploy)
    parent_inode = deploy.stat().st_ino
    original_fsync = migration.os.fsync
    parent_fsyncs = 0

    def fsync(descriptor: int) -> None:
        nonlocal parent_fsyncs
        if migration.os.fstat(descriptor).st_ino == parent_inode:
            parent_fsyncs += 1
            if parent_fsyncs == 1:
                raise OSError("first parent sync failed")
        original_fsync(descriptor)

    monkeypatch.setattr(migration.os, "fsync", fsync)
    with pytest.raises(
        migration.MigrationRefused, match="migration_journal_write_failed"
    ):
        migration._write(path, _record(migration.MigrationPhase.CREATE_REQUESTED))

    migration._write(path, _record(migration.MigrationPhase.CREATE_REQUESTED))

    assert parent_fsyncs == 2
    persisted = json.loads(path.read_text())
    assert (
        migration._parse_invocation(persisted["invocation"]).phase
        is migration.MigrationPhase.CREATE_REQUESTED
    )


def test_terminal_archive_retry_publishes_only_complete_fsynced_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / ".deployment-state"
    state.mkdir(mode=0o700)
    state.chmod(0o700)
    journal = state / "migration.json"
    record = _record(migration.MigrationPhase.COMPLETE)
    history = state / "history"
    archive = history / f"{record.identity.invocation_id}.json"
    original_fsync = migration.os.fsync
    fail_once = True

    def fsync(descriptor: int) -> None:
        nonlocal fail_once
        if stat.S_ISREG(migration.os.fstat(descriptor).st_mode) and fail_once:
            fail_once = False
            raise OSError("archive sync failed")
        original_fsync(descriptor)

    monkeypatch.setattr(migration.os, "fsync", fsync)
    with pytest.raises(
        migration.MigrationRefused, match="migration_journal_write_failed"
    ):
        migration._archive_terminal(journal, record)
    assert not archive.exists()

    monkeypatch.setattr(migration.os, "fsync", original_fsync)
    migration._archive_terminal(journal, record)
    persisted = json.loads(archive.read_text())
    assert migration._parse_invocation(persisted["invocation"]) == record


def test_safe_preddl_guard_requires_terminal_zero_restart_evidence(
    tmp_path: Path,
) -> None:
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    record = _record(migration.MigrationPhase.COMPLETE)
    migration._write(migration._journal_path(deploy), record)

    with pytest.raises(
        migration.MigrationRefused, match="migration_preddl_safety_unproved"
    ):
        migration.require_safe_preddl(deploy)


def test_candidate_heads_command_resolves_depends_on_lineages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = _binding()
    effective_heads = "691_payments_intent_correlation\nnu_0001_numbering\n"
    calls: list[tuple[str, ...]] = []

    def output(
        _binding: probe.CapturedNetworkBinding,
        *,
        image: str,
        module: str,
        arguments: tuple[str, ...],
        operation: str,
    ) -> str:
        assert image == "candidate"
        assert module == "alembic"
        assert operation == "migration-head-observation"
        calls.append(arguments)
        return effective_heads

    monkeypatch.setattr(probe, "_probe", output)

    assert migration._observe_heads(binding, image="candidate", current=False) == (
        "691_payments_intent_correlation",
        "nu_0001_numbering",
    )
    assert calls == [("heads", "--resolve-dependencies")]
    assert "pm_0003_confirmation_immutable" not in effective_heads


def test_silent_successful_predecessor_probes_prove_safe_preddl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = _binding()
    record = _record(migration.MigrationPhase.EXITED)
    calls: list[str] = []
    monkeypatch.setattr(
        migration,
        "_observe_heads",
        lambda _binding, *, image, current: (
            record.heads_before if current else record.candidate_heads
        ),
    )

    def silent_success(
        _binding: probe.CapturedNetworkBinding,
        *,
        image: str,
        module: str,
        arguments: tuple[str, ...],
        operation: str,
    ) -> str:
        assert image == "candidate"
        assert arguments == ()
        calls.append(operation)
        return ""

    monkeypatch.setattr(probe, "_probe", silent_success)

    assert (
        migration._classify_terminal_state(
            binding,
            record,
            record.terminal,  # type: ignore[arg-type]
        )
        is migration.MigrationDisposition.SAFE_PREDDL
    )
    assert calls == ["payment-email-predecessor", "payments-predecessor"]


def test_shell_candidate_heads_uses_the_canonical_graph_owner() -> None:
    script = Path(__file__).resolve().parents[1] / "scripts/deploy.sh"
    source = script.read_text(encoding="utf-8")
    start = source.index("candidate_heads() {")
    end = source.index("verify_post_migration_resume() {", start)
    command = source[start:end]

    assert "--entrypoint python" in command
    assert (
        "scripts.ci.migrated_test_database import install_migration_graph_environment"
        in command
    )
    assert "main(argv=sys.argv[1:])" in command
    assert "heads --resolve-dependencies" in command


def test_shell_cutover_checks_supported_floor_before_stop_and_after_drain() -> None:
    script = Path(__file__).resolve().parents[1] / "scripts/deploy.sh"
    source = script.read_text(encoding="utf-8")
    start = source.index("prepare_production_writer_cutover() {")
    end = source.index("resume_exact_incumbent_preddl() {", start)
    prepare = source[start:end]
    first_proof = prepare.index("verify_supported_payments_floor")
    first_stop = prepare.index("stop_captured_container_gracefully")
    final_broker_proof = prepare.index(
        "observe_writer_drain wait-for-stable-empty broker-only"
    )
    final_floor_proof = prepare.rindex("verify_supported_payments_floor")
    assert first_proof < first_stop
    assert final_broker_proof < final_floor_proof
    assert "verify_incumbent_predecessor_floors" not in prepare
    assert "verify_incumbent_predecessor_floors" in source[end:]


def test_maintenance_error_and_interrupt_handlers_never_restore_legacy_image() -> None:
    script = Path(__file__).resolve().parents[1] / "scripts/deploy.sh"
    source = script.read_text(encoding="utf-8")
    start = source.index("restore_prev() {")
    end = source.index("report_rollback_outcome() {", start)
    recovery = source[start:end]

    maintenance_guard = recovery.index('"${DOTMAC_MAINTENANCE_CONTEXT_FD:-}" == 7')
    assert maintenance_guard < recovery.index("previous_image_rollback_allowed")
    assert maintenance_guard < recovery.index("repin_prev")
    assert 'ROLLBACK_OUTCOME="maintenance_forward_only"' in recovery
    assert "trap 'if restore_prev" in source
    assert "cleanup_children; if restore_prev" in source


def _container(
    binding: probe.CapturedNetworkBinding,
    *,
    container_id: str,
    invocation_id: str,
    run_id: int,
    authorization_run_id: int,
    revision: str,
    status: str,
    exit_code: int,
    networks: dict[str, object] | None = None,
) -> dict[str, object]:
    name = f"dotmac_sub_migrate_{run_id}_{invocation_id.replace('-', '')[:16]}"
    return {
        "Id": container_id,
        "Name": f"/{name}",
        "Image": binding.candidate_image_id,
        "RestartCount": 0,
        "Config": {
            "Image": binding.candidate_image_id,
            "Entrypoint": ["/opt/venv/bin/python"],
            "Cmd": ["-I", "-B", "-m", "alembic", "upgrade", "heads"],
            "WorkingDir": "/app",
            "Env": [
                "DATABASE_URL=postgresql://u:p@postgres-local/db",
                "CELERY_BROKER_URL=redis://:p@redis-local/10",
            ],
            "Labels": {
                "com.dotmac.production-deploy.run-id": str(run_id),
                "com.dotmac.production-deploy.authorization-run-id": str(
                    authorization_run_id
                ),
                "com.dotmac.production-deploy.invocation-id": invocation_id,
                "org.opencontainers.image.revision": revision,
            },
        },
        "HostConfig": {
            "ReadonlyRootfs": True,
            "NetworkMode": binding.network_id,
            "ExtraHosts": [
                f"{binding.database_hostname}:{binding.database.ipv4_address}",
                f"{binding.broker_hostname}:{binding.broker.ipv4_address}",
            ],
            "Privileged": False,
            "AutoRemove": False,
            "CapAdd": [],
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "Binds": [],
            "VolumesFrom": [],
            "RestartPolicy": {"Name": "no"},
        },
        "NetworkSettings": {
            "Networks": networks
            if networks is not None
            else (
                {}
                if status == "created"
                else {binding.network_name: {"NetworkID": binding.network_id}}
            )
        },
        "State": {
            "Status": status,
            "StartedAt": "2026-10-15T00:00:01Z" if status != "created" else "",
            "FinishedAt": "2026-10-15T00:00:02Z" if status == "exited" else "",
            "ExitCode": exit_code,
            "OOMKilled": False,
            "Restarting": False,
        },
        "Mounts": [],
    }


@pytest.mark.parametrize(
    "networks",
    [{}, {"dotmac_default": {"NetworkID": "", "EndpointID": ""}}],
)
def test_migration_created_container_accepts_empty_docker_endpoint(
    networks: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = _binding()
    record = _record(migration.MigrationPhase.CREATED)
    value = _container(
        binding,
        container_id=record.identity.container_id or "",
        invocation_id=record.identity.invocation_id,
        run_id=record.identity.run_id,
        authorization_run_id=record.identity.authorization_run_id,
        revision=record.identity.target_revision,
        status="created",
        exit_code=0,
        networks=networks,
    )
    if networks == {}:
        value["NetworkSettings"] = {}
    monkeypatch.setattr(migration, "_inspect", lambda _identifier: value)

    assert (
        migration._inspect_exact(
            record.identity,
            binding,
            expected_status="created",
            runtime_config=(
                "postgresql://u:p@postgres-local/db",
                "redis://:p@redis-local/10",
                "CELERY_BROKER_URL",
            ),
        )
        == value
    )


@pytest.mark.parametrize(
    ("status", "networks"),
    [
        ("created", {"other": {"NetworkID": ""}}),
        ("created", {"dotmac_default": {"NetworkID": "f" * 64}}),
        ("exited", {"dotmac_default": {"NetworkID": ""}}),
    ],
)
def test_migration_rejects_wrong_or_unattached_network(
    status: str,
    networks: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = _binding()
    record = _record(migration.MigrationPhase.CREATED)
    value = _container(
        binding,
        container_id=record.identity.container_id or "",
        invocation_id=record.identity.invocation_id,
        run_id=record.identity.run_id,
        authorization_run_id=record.identity.authorization_run_id,
        revision=record.identity.target_revision,
        status=status,
        exit_code=0,
        networks=networks,
    )
    monkeypatch.setattr(migration, "_inspect", lambda _identifier: value)

    with pytest.raises(
        migration.MigrationRefused, match="migration_container_unproved"
    ):
        migration._inspect_exact(
            record.identity,
            binding,
            expected_status=status,
            runtime_config=(
                "postgresql://u:p@postgres-local/db",
                "redis://:p@redis-local/10",
                "CELERY_BROKER_URL",
            ),
        )


def _install_success_fakes(
    deploy_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    container_exit: int = 0,
    current_heads: str = "Rev: b2 (head)\n",
    predecessor_failure: str | None = None,
    image_calls: list[str] | None = None,
    start_status: str = "exited",
    installed_floor: str = "installed",
    candidate_heads_output: str = "b2 (head)\n",
    lifecycle_observations: list[tuple[migration.MigrationPhase, object]] | None = None,
) -> list[list[str]]:
    binding = _binding()
    created_id = "9" * 64
    calls: list[list[str]] = []
    state: dict[str, object] = {
        "status": "created",
        "name": "",
        "invocation_id": "",
        "run_id": "",
        "authorization_run_id": "",
        "revision": "",
        "pre_cleanup_phase": None,
        "pre_cleanup_terminal": None,
    }
    monkeypatch.setattr(
        probe,
        "_verify",
        lambda *_args, **_kwargs: (
            "postgresql://u:p@postgres-local/db",
            "redis://:p@redis-local/10",
            "CELERY_BROKER_URL",
        ),
    )
    monkeypatch.setattr(
        migration,
        "_image_details",
        lambda _binding: {
            "Id": binding.candidate_image_id,
            "Config": {"Labels": {"org.opencontainers.image.revision": "8" * 40}},
        },
    )

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[1] == "create":
            state["name"] = command[command.index("--name") + 1]
            label_values = [
                command[index + 1]
                for index, item in enumerate(command[:-1])
                if item == "--label"
            ]
            labels = dict(value.split("=", 1) for value in label_values)
            state["invocation_id"] = labels[
                "com.dotmac.production-deploy.invocation-id"
            ]
            state["run_id"] = labels["com.dotmac.production-deploy.run-id"]
            state["authorization_run_id"] = labels[
                "com.dotmac.production-deploy.authorization-run-id"
            ]
            state["revision"] = labels["org.opencontainers.image.revision"]
            return subprocess.CompletedProcess(command, 0, created_id, "")
        if command[1] == "start":
            state["status"] = start_status
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[1] == "rm":
            persisted = json.loads(
                migration._journal_path(deploy_directory).read_text()
            )
            record = migration._parse_invocation(persisted["invocation"])
            state["pre_cleanup_phase"] = record.phase
            state["pre_cleanup_terminal"] = record.terminal
            if lifecycle_observations is not None:
                lifecycle_observations.append((record.phase, record.terminal))
            return subprocess.CompletedProcess(command, 0, created_id, "")
        raise AssertionError("unexpected docker command")

    def inspect(command: list[str]) -> dict[str, object]:
        return _container(
            binding,
            container_id=created_id,
            invocation_id=str(state["invocation_id"]),
            run_id=int(str(state["run_id"])),
            authorization_run_id=int(str(state["authorization_run_id"])),
            revision=str(state["revision"]),
            status=str(state["status"]),
            exit_code=container_exit,
        )

    monkeypatch.setattr(probe, "_run", run)
    monkeypatch.setattr(migration, "_inspect", lambda identifier: inspect([identifier]))

    def output(
        _binding: probe.CapturedNetworkBinding,
        *,
        image: str,
        module: str,
        arguments: tuple[str, ...],
        operation: str,
    ) -> str:
        if image_calls is not None:
            image_calls.append(image)
        if module == "alembic" and arguments[0] == "current":
            return current_heads
        if module == "alembic":
            return candidate_heads_output
        if operation == "payments-installed":
            return json.dumps(
                {
                    "schema_version": 1,
                    "floor": installed_floor,
                    "observation": {
                        "legacy_shape_complete": True,
                        "installed_shape_complete": True,
                    },
                }
            )
        if image == "candidate" and operation.endswith("-predecessor"):
            if operation == predecessor_failure:
                raise probe.ProbeRefused("floor_unproved")
            return "predecessor-proven"
        raise AssertionError("unexpected fixed probe operation")

    monkeypatch.setattr(probe, "_probe", output)
    return calls


def _run_migration(deploy: Path) -> int:
    binding = _binding()
    return migration.run_migration(
        deploy_directory=deploy,
        binding_json=json.dumps(binding.public_json()),
        run_id=11,
        authorization_run_id=10,
        target_revision="8" * 40,
        candidate_digest="sha256:" + "7" * 64,
        heads_before=("a1",),
        candidate_heads=("b2",),
        budget_seconds=1200,
    )


def test_fixed_migration_lifecycle_records_terminal_before_nonforce_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    lifecycle_observations: list[tuple[migration.MigrationPhase, object]] = []
    calls = _install_success_fakes(
        deploy, monkeypatch, lifecycle_observations=lifecycle_observations
    )

    assert _run_migration(deploy) == 0
    assert [command[1] for command in calls] == ["create", "start", "rm"]
    assert calls[0][1] == "create" and "--rm" not in calls[0]
    assert calls[1] == ["docker", "start", "9" * 64]
    assert calls[2] == ["docker", "rm", "9" * 64]
    persisted = json.loads(migration._journal_path(deploy).read_text())
    record = migration._parse_invocation(persisted["invocation"])
    assert record.phase is migration.MigrationPhase.COMPLETE
    assert record.terminal is not None and record.terminal.exit_code == 0
    assert len(lifecycle_observations) == 1
    assert lifecycle_observations[0][0] is migration.MigrationPhase.REMOVE_REQUESTED
    terminal = lifecycle_observations[0][1]
    assert isinstance(terminal, migration.MigrationTerminalEvidence)
    assert terminal.exit_code == 0


def test_nonzero_terminal_with_same_heads_and_both_predecessors_is_safe_preddl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    image_calls: list[str] = []
    calls = _install_success_fakes(
        deploy,
        monkeypatch,
        container_exit=1,
        current_heads="Rev: a1 (head)\n",
        image_calls=image_calls,
    )

    assert _run_migration(deploy) == 1
    assert [command[1] for command in calls] == ["create", "start", "rm"]
    assert set(image_calls) == {"candidate"}
    migration.require_safe_preddl(deploy)


@pytest.mark.parametrize(
    "predecessor_failure",
    ["payments-predecessor", "payment-email-predecessor"],
)
def test_nonzero_terminal_refuses_preddl_without_both_candidate_owned_floor_readers(
    predecessor_failure: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    _install_success_fakes(
        deploy,
        monkeypatch,
        container_exit=1,
        current_heads="Rev: a1 (head)\n",
        predecessor_failure=predecessor_failure,
    )

    with pytest.raises(
        migration.MigrationRefused, match="migration_database_state_unproved"
    ):
        _run_migration(deploy)
    with pytest.raises(migration.MigrationRefused):
        migration.guard_interlock(deploy)


@pytest.mark.parametrize(
    ("installed_floor", "candidate_heads_output", "error"),
    [
        ("unknown", "b2 (head)\n", "migration_database_state_unproved"),
        ("installed", "c3 (head)\n", "candidate_heads_changed"),
    ],
)
def test_candidate_floor_or_declared_head_mismatch_never_completes(
    installed_floor: str,
    candidate_heads_output: str,
    error: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    _install_success_fakes(
        deploy,
        monkeypatch,
        installed_floor=installed_floor,
        candidate_heads_output=candidate_heads_output,
    )

    with pytest.raises(migration.MigrationRefused, match=error):
        _run_migration(deploy)
    with pytest.raises(migration.MigrationRefused):
        migration.guard_interlock(deploy)


def test_running_migration_deadline_retains_interlock_without_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    calls = _install_success_fakes(deploy, monkeypatch, start_status="running")
    times = iter((0.0, 1200.0))
    monkeypatch.setattr(migration.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(migration.time, "sleep", lambda _seconds: None)

    with pytest.raises(
        migration.MigrationRefused, match="migration_runtime_indeterminate"
    ):
        _run_migration(deploy)

    assert [command[1] for command in calls] == ["create", "start"]
    record = migration._parse_invocation(
        json.loads(migration._journal_path(deploy).read_text())["invocation"]
    )
    assert record.phase is migration.MigrationPhase.INDETERMINATE
    with pytest.raises(migration.MigrationRefused):
        migration.guard_interlock(deploy)


@pytest.mark.parametrize(
    ("failure_at", "expected_phase"),
    [
        ("create-timeout", migration.MigrationPhase.INDETERMINATE),
        ("start-timeout", migration.MigrationPhase.INDETERMINATE),
        ("cleanup-error", migration.MigrationPhase.INDETERMINATE),
    ],
)
def test_unknown_docker_outcome_retains_blocking_journal(
    failure_at: str,
    expected_phase: migration.MigrationPhase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    calls = _install_success_fakes(deploy, monkeypatch)
    original_run = probe._run

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if failure_at == "create-timeout" and command[1] == "create":
            calls.append(command)
            raise probe.ProbeIndeterminate("timed-out")
        if failure_at == "start-timeout" and command[1] == "start":
            # The exact container still says created: never retry start.
            calls.append(command)
            raise probe.ProbeIndeterminate("timed-out")
        if failure_at == "cleanup-error" and command[1] == "rm":
            calls.append(command)
            return subprocess.CompletedProcess(command, 1, "", "")
        return original_run(command, **kwargs)

    monkeypatch.setattr(probe, "_run", run)
    with pytest.raises(migration.MigrationRefused):
        _run_migration(deploy)
    record = migration._parse_invocation(
        json.loads(migration._journal_path(deploy).read_text())["invocation"]
    )
    assert record.phase is expected_phase
    expected_commands = {
        "create-timeout": ["create"],
        "start-timeout": ["create", "start"],
        "cleanup-error": ["create", "start", "rm"],
    }
    assert [command[1] for command in calls] == expected_commands[failure_at]
    with pytest.raises(migration.MigrationRefused):
        migration.guard_interlock(deploy)
