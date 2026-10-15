"""Run Alembic in one journaled, exact-identity production container.

This adapter deliberately owns only the migration invocation lifecycle. The
database floor readers and the cutover network adapter remain the authorities
for their respective observations.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final

from scripts import production_deploy_probe as probe

_CONTAINER_ID: Final = re.compile(r"^[0-9a-f]{64}$")
_RUN_ID: Final = re.compile(r"^[1-9][0-9]{0,19}$")
_SHA: Final = re.compile(r"^[0-9a-f]{40}$")
_DIGEST: Final = re.compile(r"^sha256:[0-9a-f]{64}$")
_IMAGE_ID: Final = re.compile(r"^sha256:[0-9a-f]{64}$")
_HEAD: Final = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_LABEL_RUN: Final = "com.dotmac.production-deploy.run-id"
_LABEL_AUTH: Final = "com.dotmac.production-deploy.authorization-run-id"
_LABEL_INVOCATION: Final = "com.dotmac.production-deploy.invocation-id"
_LABEL_REVISION: Final = "org.opencontainers.image.revision"
_RECORD_SCHEMA: Final = "DotmacProductionMigrationJournalV1"
_MAINTENANCE_CONTEXT_SCHEMA: Final = "DotmacMaintenanceExecutionContextV1"
_CANONICAL_LOCK: Final = Path("/var/lock/dotmac_sub_deploy.lock")
_MAINTENANCE_CONTEXT_FD: Final = 7
_CANONICAL_LOCK_FD: Final = 8


class MigrationRefused(ValueError):
    """A closed migration denial without command output or secret values."""


class MigrationPhase(StrEnum):
    CREATE_REQUESTED = "create-requested"
    CREATED = "created"
    START_REQUESTED = "start-requested"
    RUNNING = "running"
    EXITED = "exited"
    REMOVE_REQUESTED = "remove-requested"
    REMOVED = "removed"
    COMPLETE = "complete"
    SAFE_PREDDL = "safe-preddl"
    FORWARD_REPAIR_REQUIRED = "forward-repair-required"
    INDETERMINATE = "indeterminate"


class MigrationDisposition(StrEnum):
    UNRESOLVED = "unresolved"
    COMPLETE = "complete"
    SAFE_PREDDL = "safe-preddl"
    FORWARD_REPAIR_REQUIRED = "forward-repair-required"


@dataclass(frozen=True, slots=True)
class MigrationReconciliationRequest:
    deploy_directory: Path
    invocation_id: str
    journal_sha256: str

    def __post_init__(self) -> None:
        if (
            not self.deploy_directory.is_absolute()
            or self.deploy_directory.resolve() != self.deploy_directory
            or not _valid_uuid(self.invocation_id)
            or re.fullmatch(r"[0-9a-f]{64}", self.journal_sha256) is None
        ):
            raise MigrationRefused("migration_reconciliation_request_unproved")


@dataclass(frozen=True, slots=True)
class MigrationReconciliationOutcome:
    invocation_id: str
    journal_sha256: str
    disposition: MigrationDisposition
    phase: MigrationPhase


@dataclass(frozen=True, slots=True)
class MaintenanceForwardRepairAdmission:
    failed_invocation_id: str
    failed_journal_sha256: str
    target_revision: str
    target_digest: str
    target_image_id: str

    def __post_init__(self) -> None:
        if (
            not _valid_uuid(self.failed_invocation_id)
            or re.fullmatch(r"[0-9a-f]{64}", self.failed_journal_sha256) is None
            or _SHA.fullmatch(self.target_revision) is None
            or _DIGEST.fullmatch(self.target_digest) is None
            or _IMAGE_ID.fullmatch(self.target_image_id) is None
        ):
            raise MigrationRefused("maintenance_repair_admission_unproved")


@dataclass(frozen=True, slots=True)
class MaintenanceExecutionContext:
    deploy_directory: Path
    control_repository: Path
    authorization_main_revision: str
    production_authorization_path: Path
    authorization_run_id: int
    target_revision: str
    target_digest: str
    target_image_id: str
    failed_invocation_id: str
    failed_journal_sha256: str
    deployment_run_id: int

    def __post_init__(self) -> None:
        if (
            any(
                not path.is_absolute() or path.resolve() != path
                for path in (
                    self.deploy_directory,
                    self.control_repository,
                    self.production_authorization_path,
                )
            )
            or _SHA.fullmatch(self.authorization_main_revision) is None
            or type(self.authorization_run_id) is not int
            or self.authorization_run_id <= 0
            or _SHA.fullmatch(self.target_revision) is None
            or _DIGEST.fullmatch(self.target_digest) is None
            or _IMAGE_ID.fullmatch(self.target_image_id) is None
            or not _valid_uuid(self.failed_invocation_id)
            or re.fullmatch(r"[0-9a-f]{64}", self.failed_journal_sha256) is None
            or type(self.deployment_run_id) is not int
            or self.deployment_run_id <= 0
        ):
            raise MigrationRefused("maintenance_context_unproved")

    def public_json(self) -> dict[str, object]:
        return {
            "schema": _MAINTENANCE_CONTEXT_SCHEMA,
            "deploy_directory": str(self.deploy_directory),
            "control_repository": str(self.control_repository),
            "authorization_main_revision": self.authorization_main_revision,
            "production_authorization_path": str(self.production_authorization_path),
            "authorization_run_id": self.authorization_run_id,
            "target_revision": self.target_revision,
            "target_image_digest": self.target_digest,
            "target_image_id": self.target_image_id,
            "failed_invocation_id": self.failed_invocation_id,
            "failed_journal_sha256": self.failed_journal_sha256,
            "deployment_run_id": self.deployment_run_id,
        }


@dataclass(frozen=True, slots=True)
class MigrationIdentity:
    run_id: int
    authorization_run_id: int
    target_revision: str
    candidate_digest: str
    candidate_image_id: str
    invocation_id: str
    attempt: int
    container_name: str
    container_id: str | None

    def __post_init__(self) -> None:
        if (
            type(self.run_id) is not int
            or self.run_id <= 0
            or type(self.authorization_run_id) is not int
            or self.authorization_run_id <= 0
            or _SHA.fullmatch(self.target_revision) is None
            or _DIGEST.fullmatch(self.candidate_digest) is None
            or _IMAGE_ID.fullmatch(self.candidate_image_id) is None
            or not _valid_uuid(self.invocation_id)
            or type(self.attempt) is not int
            or self.attempt < 1
            or not self.container_name.startswith("dotmac_sub_migrate_")
            or any(
                character
                not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
                for character in self.container_name
            )
            or (
                self.container_id is not None
                and _CONTAINER_ID.fullmatch(self.container_id) is None
            )
        ):
            raise MigrationRefused("migration_identity_unproved")


@dataclass(frozen=True, slots=True)
class MigrationTerminalEvidence:
    container_id: str
    image_id: str
    container_name: str
    invocation_id: str
    started_at: str
    finished_at: str
    exit_code: int
    oom_killed: bool
    restart_count: int
    entrypoint: tuple[str, ...]
    command: tuple[str, ...]
    network_id: str

    def __post_init__(self) -> None:
        if (
            _CONTAINER_ID.fullmatch(self.container_id) is None
            or _IMAGE_ID.fullmatch(self.image_id) is None
            or not self.container_name.startswith("dotmac_sub_migrate_")
            or not _valid_uuid(self.invocation_id)
            or not _valid_timestamp(self.started_at)
            or not _valid_timestamp(self.finished_at)
            or type(self.exit_code) is not int
            or type(self.oom_killed) is not bool
            or type(self.restart_count) is not int
            or self.restart_count != 0
            or self.entrypoint != ("/opt/venv/bin/python",)
            or self.command != ("-I", "-B", "-m", "alembic", "upgrade", "heads")
            or _CONTAINER_ID.fullmatch(self.network_id) is None
        ):
            raise MigrationRefused("migration_terminal_unproved")
        if _timestamp(self.finished_at) < _timestamp(self.started_at):
            raise MigrationRefused("migration_terminal_unproved")


@dataclass(frozen=True, slots=True)
class MigrationInvocation:
    identity: MigrationIdentity
    phase: MigrationPhase
    heads_before: tuple[str, ...]
    candidate_heads: tuple[str, ...]
    network_binding: dict[str, object]
    terminal: MigrationTerminalEvidence | None = None
    terminal_detail: str | None = None

    def __post_init__(self) -> None:
        if (
            not self.heads_before
            or not self.candidate_heads
            or any(
                _HEAD.fullmatch(value) is None
                for value in (*self.heads_before, *self.candidate_heads)
            )
            or tuple(sorted(set(self.heads_before))) != self.heads_before
            or tuple(sorted(set(self.candidate_heads))) != self.candidate_heads
            or type(self.network_binding) is not dict
            or self.terminal_detail
            not in {None, "exit-nonzero", "timed-out", "cleanup-unproved"}
        ):
            raise MigrationRefused("migration_record_unproved")
        try:
            binding = probe._binding_from_json(json.dumps(self.network_binding))
        except (TypeError, ValueError):
            raise MigrationRefused("migration_record_unproved") from None
        if (
            self.identity.target_revision != binding.candidate_revision
            or self.identity.candidate_digest != binding.candidate_digest
            or self.identity.candidate_image_id != binding.candidate_image_id
        ):
            raise MigrationRefused("migration_record_unproved")
        if (
            self.phase
            in {
                MigrationPhase.CREATED,
                MigrationPhase.START_REQUESTED,
                MigrationPhase.RUNNING,
                MigrationPhase.EXITED,
                MigrationPhase.REMOVE_REQUESTED,
                MigrationPhase.REMOVED,
                MigrationPhase.COMPLETE,
                MigrationPhase.SAFE_PREDDL,
                MigrationPhase.FORWARD_REPAIR_REQUIRED,
            }
            and self.identity.container_id is None
        ):
            raise MigrationRefused("migration_identity_unproved")
        if self.terminal is not None and (
            self.identity.container_id != self.terminal.container_id
            or self.terminal.image_id != self.identity.candidate_image_id
            or self.terminal.container_name != self.identity.container_name
            or self.terminal.invocation_id != self.identity.invocation_id
            or self.terminal.network_id != binding.network_id
        ):
            raise MigrationRefused("migration_terminal_unproved")
        if (
            self.phase
            in {
                MigrationPhase.EXITED,
                MigrationPhase.REMOVE_REQUESTED,
                MigrationPhase.REMOVED,
                MigrationPhase.COMPLETE,
                MigrationPhase.SAFE_PREDDL,
                MigrationPhase.FORWARD_REPAIR_REQUIRED,
            }
            and self.terminal is None
        ):
            raise MigrationRefused("migration_terminal_unproved")
        if (
            self.phase is MigrationPhase.COMPLETE
            and self.terminal is not None
            and self.terminal.exit_code != 0
        ):
            raise MigrationRefused("migration_terminal_unproved")
        if self.phase is MigrationPhase.SAFE_PREDDL and (
            self.terminal is None
            or self.terminal.exit_code == 0
            or self.terminal.oom_killed
            or self.terminal.restart_count != 0
        ):
            raise MigrationRefused("migration_terminal_unproved")
        if self.phase is MigrationPhase.COMPLETE and (
            self.terminal is None
            or self.terminal.oom_killed
            or self.terminal.restart_count != 0
        ):
            raise MigrationRefused("migration_terminal_unproved")
        if (
            self.phase is MigrationPhase.FORWARD_REPAIR_REQUIRED
            and self.terminal is None
        ):
            raise MigrationRefused("migration_terminal_unproved")


def _valid_uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except (AttributeError, TypeError, ValueError):
        return False


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise MigrationRefused("migration_terminal_unproved") from None
    if parsed.tzinfo is None:
        raise MigrationRefused("migration_terminal_unproved")
    return parsed.astimezone(UTC)


def _valid_timestamp(value: str) -> bool:
    try:
        _timestamp(value)
    except MigrationRefused:
        return False
    return True


def _journal_path(deploy_directory: Path) -> Path:
    if (
        not deploy_directory.is_absolute()
        or deploy_directory.resolve() != deploy_directory
    ):
        raise MigrationRefused("deploy_directory_unproved")
    return deploy_directory / ".deployment-state" / "migration.json"


def _canonical(value: dict[str, object]) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode()


def read_maintenance_context(
    descriptor: int = _MAINTENANCE_CONTEXT_FD,
) -> MaintenanceExecutionContext | None:
    try:
        fd_metadata = os.fstat(descriptor)
    except OSError:
        return None
    if (
        not stat.S_ISREG(fd_metadata.st_mode)
        or fd_metadata.st_uid != os.geteuid()
        or stat.S_IMODE(fd_metadata.st_mode) != 0o600
        or fd_metadata.st_nlink != 0
        or fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDONLY
    ):
        raise MigrationRefused("maintenance_context_unproved")
    try:
        lock_metadata = os.fstat(_CANONICAL_LOCK_FD)
        path_metadata = _CANONICAL_LOCK.lstat()
        fcntl.flock(_CANONICAL_LOCK_FD, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (
            not stat.S_ISREG(lock_metadata.st_mode)
            or lock_metadata.st_dev != path_metadata.st_dev
            or lock_metadata.st_ino != path_metadata.st_ino
            or stat.S_IMODE(lock_metadata.st_mode) != 0o600
        ):
            raise MigrationRefused("maintenance_context_lock_unproved")
        raw = os.pread(descriptor, 65_537, 0)
        if not raw or len(raw) > 65_536:
            raise ValueError
        document = json.loads(raw)
        if (
            type(document) is not dict
            or raw != _canonical(document)
            or set(document)
            != {
                "schema",
                "deploy_directory",
                "control_repository",
                "authorization_main_revision",
                "production_authorization_path",
                "authorization_run_id",
                "target_revision",
                "target_image_digest",
                "target_image_id",
                "failed_invocation_id",
                "failed_journal_sha256",
                "deployment_run_id",
            }
            or document.get("schema") != _MAINTENANCE_CONTEXT_SCHEMA
        ):
            raise ValueError
        return MaintenanceExecutionContext(
            Path(document["deploy_directory"]),
            Path(document["control_repository"]),
            document["authorization_main_revision"],
            Path(document["production_authorization_path"]),
            document["authorization_run_id"],
            document["target_revision"],
            document["target_image_digest"],
            document["target_image_id"],
            document["failed_invocation_id"],
            document["failed_journal_sha256"],
            document["deployment_run_id"],
        )
    except MigrationRefused:
        raise
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
        raise MigrationRefused("maintenance_context_unproved") from None


def _ensure_state_directory(path: Path, *, create: bool) -> None:
    directory = path.parent
    if create:
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        except OSError:
            raise MigrationRefused("migration_journal_unavailable") from None
    try:
        metadata = directory.lstat()
    except OSError:
        if not create and not directory.exists():
            return
        raise MigrationRefused("migration_journal_unavailable") from None
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise MigrationRefused("migration_journal_unavailable")
    if create:
        try:
            parent_fd = os.open(
                directory.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        except OSError:
            raise MigrationRefused("migration_journal_write_failed") from None


def _write(path: Path, record: MigrationInvocation) -> None:
    _ensure_state_directory(path, create=True)
    payload = _canonical(
        {"schema": _RECORD_SCHEMA, "invocation": _invocation_json(record)}
    )
    temporary = path.with_name(f".{path.name}.{record.identity.invocation_id}.tmp")
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
            directory_fd = os.open(
                path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary.exists():
                temporary.unlink()
    except OSError:
        raise MigrationRefused("migration_journal_write_failed") from None


def _archive_terminal(path: Path, record: MigrationInvocation) -> None:
    """Retain a completed invocation before a reviewed repair supersedes it."""
    if (
        record.phase
        not in {
            MigrationPhase.COMPLETE,
            MigrationPhase.SAFE_PREDDL,
            MigrationPhase.FORWARD_REPAIR_REQUIRED,
        }
        or record.terminal is None
    ):
        raise MigrationRefused("migration_journal_unproved")
    directory = path.parent / "history"
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError:
        raise MigrationRefused("migration_journal_write_failed") from None
    temporary: Path | None = None
    try:
        metadata = directory.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise MigrationRefused("migration_journal_unavailable")
        payload = _canonical(
            {"schema": _RECORD_SCHEMA, "invocation": _invocation_json(record)}
        )
        archive = directory / f"{record.identity.invocation_id}.json"
        parent_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{record.identity.invocation_id}.",
                suffix=".tmp",
                dir=directory,
            )
            temporary = Path(temporary_name)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, archive, follow_symlinks=False)
        except FileExistsError:
            try:
                archive_metadata = archive.lstat()
                if (
                    not stat.S_ISREG(archive_metadata.st_mode)
                    or archive_metadata.st_uid != os.geteuid()
                    or stat.S_IMODE(archive_metadata.st_mode) != 0o600
                ):
                    raise OSError
                existing = archive.read_bytes()
                if existing != payload:
                    raise MigrationRefused("migration_journal_unproved")
                archive_fd = os.open(
                    archive, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                )
                try:
                    os.fsync(archive_fd)
                finally:
                    os.close(archive_fd)
            except OSError:
                raise MigrationRefused("migration_journal_unproved") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        history_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(history_fd)
        finally:
            os.close(history_fd)
        parent_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except MigrationRefused:
        raise
    except OSError:
        raise MigrationRefused("migration_journal_write_failed") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _invocation_json(value: MigrationInvocation) -> dict[str, object]:
    return {
        "identity": asdict(value.identity),
        "phase": value.phase.value,
        "heads_before": list(value.heads_before),
        "candidate_heads": list(value.candidate_heads),
        "network_binding": value.network_binding,
        "terminal": asdict(value.terminal) if value.terminal is not None else None,
        "terminal_detail": value.terminal_detail,
    }


def _load_journal(path: Path) -> tuple[MigrationInvocation | None, str | None]:
    """Strictly load canonical journal data without deciding admission."""
    _ensure_state_directory(path, create=False)
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise MigrationRefused("migration_journal_unavailable")
        raw = path.read_bytes()
        document = json.loads(raw)
        if (
            type(document) is not dict
            or set(document) != {"schema", "invocation"}
            or raw != _canonical(document)
            or document.get("schema") != _RECORD_SCHEMA
        ):
            raise MigrationRefused("migration_journal_unproved")
        raw_invocation = document.get("invocation")
        if type(raw_invocation) is not dict:
            raise MigrationRefused("migration_journal_unproved")
        invocation = _parse_invocation(raw_invocation)
        return invocation, hashlib.sha256(raw).hexdigest()
    except FileNotFoundError:
        return None, None
    except MigrationRefused:
        raise
    except (OSError, TypeError, ValueError, KeyError):
        raise MigrationRefused("migration_journal_unproved") from None


def _read(path: Path) -> MigrationInvocation | None:
    invocation, _digest = _load_journal(path)
    if invocation is not None and invocation.phase not in {
        MigrationPhase.COMPLETE,
        MigrationPhase.SAFE_PREDDL,
    }:
        raise MigrationRefused("migration_interlock_pending")
    return invocation


def _parse_invocation(value: dict[str, object]) -> MigrationInvocation:
    try:
        if set(value) != {
            "identity",
            "phase",
            "heads_before",
            "candidate_heads",
            "network_binding",
            "terminal",
            "terminal_detail",
        }:
            raise ValueError
        parsed_binding = probe._binding_from_json(json.dumps(value["network_binding"]))
        # Old schema-1 artifacts admitted only empty ExtraHosts. Preserve that
        # exact meaning; never infer a newly observed nonempty mapping.
        compatible_binding = dict(value["network_binding"])
        compatible_binding.setdefault("app_extra_hosts", {})
        compatible_writers: list[dict[str, object]] = []
        for writer in compatible_binding["writers"]:
            compatible_writer = dict(writer)
            compatible_writer.setdefault("extra_hosts", {})
            compatible_writers.append(compatible_writer)
        compatible_binding["writers"] = compatible_writers
        if parsed_binding.public_json() != compatible_binding:
            raise ValueError
        identity = MigrationIdentity(**value["identity"])
        phase = MigrationPhase(value["phase"])
        terminal_value = value["terminal"]
        terminal = None
        if terminal_value is not None:
            terminal_value["entrypoint"] = tuple(terminal_value["entrypoint"])
            terminal_value["command"] = tuple(terminal_value["command"])
            terminal = MigrationTerminalEvidence(**terminal_value)
        return MigrationInvocation(
            identity=identity,
            phase=phase,
            heads_before=tuple(value["heads_before"]),
            candidate_heads=tuple(value["candidate_heads"]),
            network_binding=value["network_binding"],
            terminal=terminal,
            terminal_detail=value["terminal_detail"],
        )
    except (KeyError, TypeError, ValueError):
        raise MigrationRefused("migration_journal_unproved") from None


def require_safe_preddl(deploy_directory: Path) -> None:
    record = _read(_journal_path(deploy_directory))
    if record is None or record.phase is not MigrationPhase.SAFE_PREDDL:
        raise MigrationRefused("migration_preddl_safety_unproved")


def guard_interlock(deploy_directory: Path) -> None:
    """Run under the canonical deploy lock before any staged wrapper."""
    _read(_journal_path(deploy_directory))


def _reconciliation_outcome(
    request: MigrationReconciliationRequest,
    disposition: MigrationDisposition,
    phase: MigrationPhase,
) -> MigrationReconciliationOutcome:
    _record, digest = _load_journal(_journal_path(request.deploy_directory))
    if digest is None:
        raise MigrationRefused("migration_journal_unproved")
    return MigrationReconciliationOutcome(
        request.invocation_id, digest, disposition, phase
    )


def _exact_container_absence_proven(identity: MigrationIdentity) -> bool:
    if identity.container_id is None:
        return False
    by_id = probe._run(
        [
            "docker",
            "container",
            "ls",
            "-a",
            "--no-trunc",
            "--filter",
            f"id={identity.container_id}",
            "--format",
            "{{.ID}}",
        ]
    )
    by_name = probe._run(
        [
            "docker",
            "container",
            "ls",
            "-a",
            "--no-trunc",
            "--filter",
            f"name=^/{identity.container_name}$",
            "--format",
            "{{.ID}} {{.Names}}",
        ]
    )
    if by_id.returncode != 0 or by_name.returncode != 0:
        return False
    ids = by_id.stdout.splitlines()
    names = by_name.stdout.splitlines()
    return not ids and not names


def _captured_writers_stopped(binding: probe.CapturedNetworkBinding) -> bool:
    app = probe._inspect(binding.app_container_id)
    _environment, app_state, _settings = probe._container_parts(app)
    if app_state.get("Running") is not False or app_state.get("Status") != "exited":
        return False
    for expected in binding.writers:
        value = probe._inspect(expected.container_id)
        _environment, state, _settings = probe._container_parts(value)
        if state.get("Running") is not False or state.get("Status") != "exited":
            return False
    return True


def _classify_terminal_state(
    binding: probe.CapturedNetworkBinding,
    record: MigrationInvocation,
    terminal: MigrationTerminalEvidence,
) -> MigrationDisposition:
    """Classify a terminal runner using only the fixed read-only probe set.

    These bounded probe containers may be created and cleaned up to observe
    Alembic heads and schema floors. This function never starts, retries,
    removes, or kills the journaled migration container or any writer.
    """
    if terminal.oom_killed or terminal.restart_count != 0:
        return MigrationDisposition.FORWARD_REPAIR_REQUIRED
    try:
        current_heads = _observe_heads(binding, image="candidate", current=True)
        candidate_heads = _observe_heads(binding, image="candidate", current=False)
        if candidate_heads != record.candidate_heads:
            return MigrationDisposition.FORWARD_REPAIR_REQUIRED
        if terminal.exit_code == 0 and current_heads == record.candidate_heads:
            installed = probe._probe(
                binding,
                image="candidate",
                module="scripts.verify_payments_intent_rollback",
                arguments=("inspect",),
                operation="payments-installed",
            )
            evidence = probe._validate_payments_floor_output(
                "payments-installed", installed
            )
            observation = evidence["observation"]
            if (
                type(observation) is dict
                and observation.get("legacy_shape_complete") is True
                and observation.get("installed_shape_complete") is True
            ):
                return MigrationDisposition.COMPLETE
        if terminal.exit_code != 0 and current_heads == record.heads_before:
            probe._probe(
                binding,
                image="candidate",
                module="scripts.verify_payment_email_rollback",
                arguments=(),
                operation="payment-email-predecessor",
            )
            probe._probe(
                binding,
                image="candidate",
                module="scripts.verify_payments_intent_rollback",
                arguments=(),
                operation="payments-predecessor",
            )
            return MigrationDisposition.SAFE_PREDDL
        return MigrationDisposition.FORWARD_REPAIR_REQUIRED
    except (MigrationRefused, probe.ProbeRefused, OSError, ValueError):
        return MigrationDisposition.UNRESOLVED


def reconcile_pending(
    request: MigrationReconciliationRequest,
) -> MigrationReconciliationOutcome:
    """Observe one retained invocation without mutating its or writer containers.

    Terminal DB classification may execute the separately fixed, read-only
    candidate probe operations; it never starts, retries, removes, or kills
    the retained migration container, nor starts a writer or deployment.
    """
    path = _journal_path(request.deploy_directory)
    record, digest = _load_journal(path)
    if (
        record is None
        or digest != request.journal_sha256
        or record.identity.invocation_id != request.invocation_id
    ):
        raise MigrationRefused("migration_reconciliation_stale")
    if record.phase in {MigrationPhase.COMPLETE, MigrationPhase.SAFE_PREDDL}:
        disposition = (
            MigrationDisposition.COMPLETE
            if record.phase is MigrationPhase.COMPLETE
            else MigrationDisposition.SAFE_PREDDL
        )
        return MigrationReconciliationOutcome(
            request.invocation_id, digest, disposition, record.phase
        )
    try:
        binding = probe._binding_from_json(json.dumps(record.network_binding))
        runtime_config = probe._verify(binding, allow_stopped_app=True)
        if not _captured_writers_stopped(binding):
            return MigrationReconciliationOutcome(
                request.invocation_id,
                digest,
                MigrationDisposition.UNRESOLVED,
                record.phase,
            )
    except (probe.ProbeRefused, OSError, ValueError):
        return MigrationReconciliationOutcome(
            request.invocation_id, digest, MigrationDisposition.UNRESOLVED, record.phase
        )

    identity = record.identity
    if identity.container_id is None:
        if record.phase not in {
            MigrationPhase.CREATE_REQUESTED,
            MigrationPhase.INDETERMINATE,
        }:
            return MigrationReconciliationOutcome(
                request.invocation_id,
                digest,
                MigrationDisposition.UNRESOLVED,
                record.phase,
            )
        try:
            value = probe._inspect_named(identity.container_name)
            if value.get("Name") != f"/{identity.container_name}":
                raise MigrationRefused("migration_container_unproved")
            container_id = value.get("Id")
            if (
                type(container_id) is not str
                or _CONTAINER_ID.fullmatch(container_id) is None
            ):
                raise MigrationRefused("migration_container_unproved")
            identity = MigrationIdentity(
                identity.run_id,
                identity.authorization_run_id,
                identity.target_revision,
                identity.candidate_digest,
                identity.candidate_image_id,
                identity.invocation_id,
                identity.attempt,
                identity.container_name,
                container_id,
            )
            _inspect_exact(
                identity,
                binding,
                expected_status="created",
                runtime_config=runtime_config,
            )
            updated = _write_phase(
                path,
                record,
                MigrationPhase.CREATED,
                identity=identity,
                terminal_detail=None,
            )
            _record, updated_digest = _load_journal(path)
            return MigrationReconciliationOutcome(
                identity.invocation_id,
                updated_digest or digest,
                MigrationDisposition.UNRESOLVED,
                updated.phase,
            )
        except (MigrationRefused, probe.ProbeRefused, OSError, ValueError):
            return MigrationReconciliationOutcome(
                request.invocation_id,
                digest,
                MigrationDisposition.UNRESOLVED,
                record.phase,
            )

    try:
        value = _inspect(identity.container_id)
        state = value.get("State")
        if type(state) is not dict:
            raise MigrationRefused("migration_container_unproved")
        status = state.get("Status")
        if status not in {"created", "running", "exited"}:
            raise MigrationRefused("migration_container_unproved")
        exact = _inspect_exact(
            identity,
            binding,
            expected_status=status,
            runtime_config=runtime_config,
        )
        if status == "created":
            if (
                record.phase
                not in {
                    MigrationPhase.CREATE_REQUESTED,
                    MigrationPhase.CREATED,
                }
                or record.terminal is not None
            ):
                return MigrationReconciliationOutcome(
                    request.invocation_id,
                    digest,
                    MigrationDisposition.UNRESOLVED,
                    record.phase,
                )
            if record.phase is MigrationPhase.CREATE_REQUESTED:
                updated = _write_phase(
                    path, record, MigrationPhase.CREATED, identity=identity
                )
                _record, updated_digest = _load_journal(path)
                return MigrationReconciliationOutcome(
                    request.invocation_id,
                    updated_digest or digest,
                    MigrationDisposition.UNRESOLVED,
                    updated.phase,
                )
            return MigrationReconciliationOutcome(
                request.invocation_id,
                digest,
                MigrationDisposition.UNRESOLVED,
                record.phase,
            )
        if status == "running":
            if (
                record.phase
                not in {
                    MigrationPhase.START_REQUESTED,
                    MigrationPhase.RUNNING,
                    MigrationPhase.INDETERMINATE,
                }
                or record.terminal is not None
            ):
                return MigrationReconciliationOutcome(
                    request.invocation_id,
                    digest,
                    MigrationDisposition.UNRESOLVED,
                    record.phase,
                )
            updated = _write_phase(
                path, record, MigrationPhase.RUNNING, identity=identity
            )
            _record, updated_digest = _load_journal(path)
            return MigrationReconciliationOutcome(
                request.invocation_id,
                updated_digest or digest,
                MigrationDisposition.UNRESOLVED,
                updated.phase,
            )
        if record.phase not in {
            MigrationPhase.START_REQUESTED,
            MigrationPhase.RUNNING,
            MigrationPhase.INDETERMINATE,
            MigrationPhase.EXITED,
            MigrationPhase.REMOVE_REQUESTED,
            MigrationPhase.REMOVED,
            MigrationPhase.COMPLETE,
            MigrationPhase.SAFE_PREDDL,
            MigrationPhase.FORWARD_REPAIR_REQUIRED,
        }:
            return MigrationReconciliationOutcome(
                request.invocation_id,
                digest,
                MigrationDisposition.UNRESOLVED,
                record.phase,
            )
        observed_terminal = _terminal(identity, binding, exact)
        if record.terminal is not None and record.terminal != observed_terminal:
            return MigrationReconciliationOutcome(
                request.invocation_id,
                digest,
                MigrationDisposition.UNRESOLVED,
                record.phase,
            )
        exited = _write_phase(
            path,
            record,
            MigrationPhase.EXITED,
            identity=identity,
            terminal=observed_terminal,
        )
    except (MigrationRefused, probe.ProbeRefused, OSError, ValueError):
        if record.terminal is None or not _exact_container_absence_proven(identity):
            return MigrationReconciliationOutcome(
                request.invocation_id,
                digest,
                MigrationDisposition.UNRESOLVED,
                record.phase,
            )
        observed_terminal = record.terminal
        exited = record

    try:
        # Recheck the captured runtime after container observation; no stale
        # identity evidence can authorize classifying the retained terminal.
        probe._verify(binding, allow_stopped_app=True)
        if not _captured_writers_stopped(binding):
            raise MigrationRefused("migration_cutover_identity_unproved")
    except (MigrationRefused, probe.ProbeRefused, OSError, ValueError):
        _record, current_digest = _load_journal(path)
        return MigrationReconciliationOutcome(
            request.invocation_id,
            current_digest or digest,
            MigrationDisposition.UNRESOLVED,
            exited.phase,
        )
    disposition = _classify_terminal_state(binding, exited, observed_terminal)
    phase = {
        MigrationDisposition.COMPLETE: MigrationPhase.COMPLETE,
        MigrationDisposition.SAFE_PREDDL: MigrationPhase.SAFE_PREDDL,
        MigrationDisposition.FORWARD_REPAIR_REQUIRED: MigrationPhase.FORWARD_REPAIR_REQUIRED,
        MigrationDisposition.UNRESOLVED: MigrationPhase.INDETERMINATE,
    }[disposition]
    if phase is not exited.phase or exited.terminal != observed_terminal:
        exited = _write_phase(
            path,
            exited,
            phase,
            identity=identity,
            terminal=observed_terminal,
        )
    _record, final_digest = _load_journal(path)
    return MigrationReconciliationOutcome(
        request.invocation_id,
        final_digest or digest,
        disposition,
        exited.phase,
    )


def validate_maintenance_admission(
    context: MaintenanceExecutionContext,
    binding: probe.CapturedNetworkBinding,
) -> MaintenanceForwardRepairAdmission:
    """Revalidate every maintenance fact from trusted references and live state."""
    if (
        context.target_revision != binding.candidate_revision
        or context.target_digest != binding.candidate_digest
        or context.target_image_id != binding.candidate_image_id
    ):
        raise MigrationRefused("maintenance_context_invocation_mismatch")
    try:
        from scripts.production_trusted_rollback_preflight import (
            TrustedPreflightInput,
            _classify_transition,
            _verify_authorization,
        )
        from scripts.release_artifact_contract import (
            GitCommitSha,
            OCIImageDigest,
            WorkflowRunId,
        )

        preflight = TrustedPreflightInput(
            production_authorization_path=context.production_authorization_path,
            authorization_run_id=WorkflowRunId(context.authorization_run_id),
            authorization_main_revision=GitCommitSha(
                context.authorization_main_revision
            ),
            target_revision=GitCommitSha(context.target_revision),
            target_image_digest=OCIImageDigest(context.target_digest),
            deploy_directory=context.deploy_directory,
            rollback_authorization_path=None,
            bootstrap_authorization_path=None,
            control_repository=context.control_repository,
        )
        _verify_authorization(preflight)
        image_id, digest, _environment = probe._candidate(
            f"{probe._REPOSITORY}@{context.target_digest}",
            context.target_revision,
        )
        if image_id != context.target_image_id or digest != context.target_digest:
            raise MigrationRefused("maintenance_target_image_unproved")
        path = _journal_path(context.deploy_directory)
        record, journal_digest = _load_journal(path)
        if (
            record is None
            or journal_digest != context.failed_journal_sha256
            or record.identity.invocation_id != context.failed_invocation_id
            or record.phase
            not in {
                MigrationPhase.FORWARD_REPAIR_REQUIRED,
                MigrationPhase.COMPLETE,
            }
            or record.terminal is None
            or record.terminal.oom_killed
            or record.terminal.restart_count != 0
            or (
                record.phase is MigrationPhase.COMPLETE
                and record.terminal.exit_code != 0
            )
        ):
            raise MigrationRefused("maintenance_journal_unproved")
        transition = _classify_transition(
            GitCommitSha(record.identity.target_revision),
            GitCommitSha(context.target_revision),
            repository=context.control_repository,
        )
        if transition.value not in {"same", "forward"} or (
            transition.value == "same"
            and record.identity.candidate_digest != context.target_digest
        ):
            raise MigrationRefused("maintenance_source_transition_unproved")
        previous_binding = probe._binding_from_json(json.dumps(record.network_binding))
        if (
            binding.network_id != previous_binding.network_id
            or binding.network_name != previous_binding.network_name
            or binding.app_container_id != previous_binding.app_container_id
            or binding.app_image_id != previous_binding.app_image_id
            or binding.app_digest != previous_binding.app_digest
            or binding.app_revision != previous_binding.app_revision
            or binding.app_started_at != previous_binding.app_started_at
            or binding.app_restart_count != previous_binding.app_restart_count
            or binding.database != previous_binding.database
            or binding.broker != previous_binding.broker
            or binding.writers != previous_binding.writers
            or binding.inactive_services != previous_binding.inactive_services
            or binding.compose_arguments != previous_binding.compose_arguments
        ):
            raise MigrationRefused("maintenance_binding_changed")
        previous_runtime = probe._verify(previous_binding, allow_stopped_app=True)
        known_terminal: tuple[str, ...] = ()
        terminal_id = record.terminal.container_id
        try:
            _inspect(terminal_id)
            exact_terminal = _inspect_exact(
                record.identity,
                previous_binding,
                expected_status="exited",
                runtime_config=previous_runtime,
            )
            if (
                _terminal(record.identity, previous_binding, exact_terminal)
                != record.terminal
            ):
                raise MigrationRefused("maintenance_migration_terminal_changed")
            known_terminal = (terminal_id,)
        except (MigrationRefused, probe.ProbeRefused, OSError):
            if not _exact_container_absence_proven(record.identity):
                raise MigrationRefused(
                    "maintenance_migration_terminal_unproved"
                ) from None
        probe._verify(binding, allow_stopped_app=True)
        if not _captured_writers_stopped(binding):
            raise MigrationRefused("maintenance_writers_not_stopped")
        probe.verify_fixed_compose_inventory(binding, known_terminal_ids=known_terminal)
        current_heads = _observe_heads(binding, image="candidate", current=True)
        if current_heads != record.candidate_heads:
            raise MigrationRefused("maintenance_heads_unproved")
        installed = probe._probe(
            binding,
            image="candidate",
            module="scripts.verify_payments_intent_rollback",
            arguments=("inspect",),
            operation="payments-installed",
        )
        floor = probe._validate_payments_floor_output("payments-installed", installed)
        observation = floor["observation"]
        if (
            type(observation) is not dict
            or observation.get("legacy_shape_complete") is not True
            or observation.get("installed_shape_complete") is not True
        ):
            raise MigrationRefused("maintenance_floor_unproved")
        drain = probe._probe(
            binding,
            image="candidate",
            module="scripts.observe_writer_drain",
            arguments=(
                "wait-for-stable-empty",
                "--mode",
                "broker-only",
                "--drain-budget",
                "600",
            ),
            operation="drain",
        )
        _validate_stable_broker_empty(drain)
        probe._verify(binding, allow_stopped_app=True)
        if not _captured_writers_stopped(binding):
            raise MigrationRefused("maintenance_writers_not_stopped")
        probe.verify_fixed_compose_inventory(binding, known_terminal_ids=known_terminal)
        return MaintenanceForwardRepairAdmission(
            context.failed_invocation_id,
            context.failed_journal_sha256,
            context.target_revision,
            context.target_digest,
            context.target_image_id,
        )
    except MigrationRefused:
        raise
    except Exception:
        raise MigrationRefused("maintenance_admission_unproved") from None


def prepare_maintenance_context(
    context: MaintenanceExecutionContext,
) -> tuple[probe.CapturedNetworkBinding, tuple[str, ...], tuple[str, ...]]:
    path = _journal_path(context.deploy_directory)
    record, digest = _load_journal(path)
    if (
        record is None
        or digest != context.failed_journal_sha256
        or record.identity.invocation_id != context.failed_invocation_id
    ):
        raise MigrationRefused("maintenance_journal_stale")
    old_binding = probe._binding_from_json(json.dumps(record.network_binding))
    image_id, image_digest, _environment = probe._candidate(
        f"{probe._REPOSITORY}@{context.target_digest}", context.target_revision
    )
    if image_id != context.target_image_id or image_digest != context.target_digest:
        raise MigrationRefused("maintenance_target_image_unproved")
    binding = replace(
        old_binding,
        candidate_image_id=image_id,
        candidate_digest=image_digest,
        candidate_revision=context.target_revision,
    )
    validate_maintenance_admission(context, binding)
    candidate_heads = _observe_heads(binding, image="candidate", current=False)
    return binding, record.candidate_heads, candidate_heads


def _validate_stable_broker_empty(output: str) -> None:
    try:
        payload = json.loads(output)

        def zero(value: object) -> bool:
            if type(value) is not dict or value.get("mode") != "broker-only":
                return False
            queues = value.get("queues")
            unacknowledged = value.get("unacknowledged")
            return (
                type(queues) is list
                and len(queues) == 11
                and len({item.get("queue") for item in queues if type(item) is dict})
                == 11
                and all(
                    type(item) is dict
                    and type(item.get("ready")) is int
                    and item["ready"] == 0
                    for item in queues
                )
                and type(unacknowledged) is dict
                and type(unacknowledged.get("hash_entries")) is int
                and unacknowledged["hash_entries"] == 0
                and type(unacknowledged.get("index_entries")) is int
                and unacknowledged["index_entries"] == 0
            )

        if (
            type(payload) is not dict
            or payload.get("schema_version") != 1
            or payload.get("mode") != "broker-only"
            or payload.get("outcome") != "stable-empty"
            or payload.get("refusal") is not None
            or not zero(payload.get("observation"))
            or not zero(payload.get("previous_zero"))
        ):
            raise ValueError
    except (TypeError, ValueError, KeyError):
        raise MigrationRefused("maintenance_broker_empty_unproved") from None


def _image_details(binding: probe.CapturedNetworkBinding) -> dict[str, object]:
    image = probe._json(
        [
            "docker",
            "image",
            "inspect",
            "--format",
            "{{json .}}",
            binding.candidate_image_id,
        ]
    )
    if type(image) is not dict or image.get("Id") != binding.candidate_image_id:
        raise MigrationRefused("migration_image_unproved")
    return image


def _inspect(identifier: str) -> dict[str, object]:
    value = probe._json(["docker", "inspect", "--format", "{{json .}}", identifier])
    if type(value) is not dict or value.get("Id") != identifier:
        raise MigrationRefused("migration_container_unproved")
    return value


def _network_attachment_is_proven(
    settings: object,
    binding: probe.CapturedNetworkBinding,
    *,
    expected_status: str,
) -> bool:
    if type(settings) is not dict:
        return False
    networks = settings.get("Networks")
    if expected_status == "created":
        if networks is None or networks == {}:
            return True
        if type(networks) is not dict or set(networks) != {binding.network_name}:
            return False
        endpoint = networks[binding.network_name]
        return type(endpoint) is dict and endpoint.get("NetworkID") in (
            "",
            binding.network_id,
        )
    return (
        type(networks) is dict
        and set(networks) == {binding.network_name}
        and type(networks[binding.network_name]) is dict
        and networks[binding.network_name].get("NetworkID") == binding.network_id
    )


def _inspect_exact(
    identity: MigrationIdentity,
    binding: probe.CapturedNetworkBinding,
    *,
    expected_status: str,
    runtime_config: tuple[str, str, str],
) -> dict[str, object]:
    value = _inspect(identity.container_id or identity.container_name)
    config = value.get("Config")
    host = value.get("HostConfig")
    state = value.get("State")
    settings = value.get("NetworkSettings")
    network_proven = _network_attachment_is_proven(
        settings, binding, expected_status=expected_status
    )
    labels = config.get("Labels") if type(config) is dict else None
    entrypoint = config.get("Entrypoint") if type(config) is dict else None
    command = config.get("Cmd") if type(config) is dict else None
    environment = config.get("Env") if type(config) is dict else None
    expected_hosts = {
        binding.database_hostname: binding.database.ipv4_address,
        binding.broker_hostname: binding.broker.ipv4_address,
    }
    expected_extra_hosts = [
        f"{hostname}:{address}" for hostname, address in sorted(expected_hosts.items())
    ]
    database_url, broker_url, broker_key = runtime_config
    environment_map: dict[str, str] = {}
    if type(environment) is list:
        for item in environment:
            if type(item) is not str:
                raise MigrationRefused("migration_container_unproved")
            key, separator, env_value = item.partition("=")
            if not separator or key in environment_map:
                raise MigrationRefused("migration_container_unproved")
            environment_map[key] = env_value
    if (
        type(config) is not dict
        or type(host) is not dict
        or type(state) is not dict
        or not network_proven
        or type(labels) is not dict
        or labels.get(_LABEL_RUN) != str(identity.run_id)
        or labels.get(_LABEL_AUTH) != str(identity.authorization_run_id)
        or labels.get(_LABEL_INVOCATION) != identity.invocation_id
        or labels.get(_LABEL_REVISION) != identity.target_revision
        or value.get("Image") != identity.candidate_image_id
        or config.get("Image") != identity.candidate_image_id
        or value.get("Name") != f"/{identity.container_name}"
        or state.get("Status") != expected_status
        or state.get("Restarting") is True
        or value.get("RestartCount") != 0
        or entrypoint != ["/opt/venv/bin/python"]
        or command != ["-I", "-B", "-m", "alembic", "upgrade", "heads"]
        or host.get("ReadonlyRootfs") is not True
        or host.get("NetworkMode") != binding.network_id
        or host.get("ExtraHosts") != expected_extra_hosts
        or host.get("Privileged") is not False
        or host.get("AutoRemove") is not False
        or host.get("CapAdd") not in (None, [])
        or type(host.get("CapDrop")) is not list
        or "ALL" not in host["CapDrop"]
        or "no-new-privileges:true" not in host.get("SecurityOpt", [])
        or host.get("Binds") not in (None, [])
        or host.get("VolumesFrom") not in (None, [])
        or type(host.get("RestartPolicy", {})) is not dict
        or (host.get("RestartPolicy") or {}).get("Name", "") not in ("", "no")
        or value.get("Mounts") not in ([], None)
        or environment_map.get("DATABASE_URL") != database_url
        or environment_map.get(broker_key) != broker_url
        or any(environment_map.get(key) for key in probe._OVERLAY_KEYS)
        or any(environment_map.get(key) for key in ("LD_PRELOAD", "LD_LIBRARY_PATH"))
    ):
        raise MigrationRefused("migration_container_unproved")
    return value


def _terminal(
    identity: MigrationIdentity,
    binding: probe.CapturedNetworkBinding,
    value: dict[str, object],
) -> MigrationTerminalEvidence:
    config = value["Config"]
    state = value["State"]
    return MigrationTerminalEvidence(
        container_id=identity.container_id or "",
        image_id=value.get("Image", ""),
        container_name=identity.container_name,
        invocation_id=identity.invocation_id,
        started_at=state.get("StartedAt", ""),
        finished_at=state.get("FinishedAt", ""),
        exit_code=state.get("ExitCode", -1),
        oom_killed=state.get("OOMKilled"),
        restart_count=value.get("RestartCount", -1),
        entrypoint=tuple(config.get("Entrypoint", [])),
        command=tuple(config.get("Cmd", [])),
        network_id=binding.network_id,
    )


def _observe_heads(
    binding: probe.CapturedNetworkBinding, *, image: str, current: bool
) -> tuple[str, ...]:
    output = probe._probe(
        binding,
        image=image,
        module="alembic",
        arguments=("current", "--verbose")
        if current
        else ("heads", "--resolve-dependencies"),
        operation="migration-head-observation",
    )
    if current:
        values = re.findall(r"^Rev:\s+([A-Za-z0-9_.-]+)", output, flags=re.MULTILINE)
    else:
        values = [line.split()[0] for line in output.splitlines() if line.strip()]
    normalized = tuple(sorted(set(values)))
    if not normalized or any(_HEAD.fullmatch(item) is None for item in normalized):
        raise MigrationRefused("migration_heads_unproved")
    return normalized


def _write_phase(
    path: Path, record: MigrationInvocation, phase: MigrationPhase, **updates: object
) -> MigrationInvocation:
    value = MigrationInvocation(
        identity=updates.get("identity", record.identity),
        phase=phase,
        heads_before=record.heads_before,
        candidate_heads=record.candidate_heads,
        network_binding=record.network_binding,
        terminal=updates.get("terminal", record.terminal),
        terminal_detail=updates.get("terminal_detail", record.terminal_detail),
    )
    _write(path, value)
    return value


def run_migration(
    *,
    deploy_directory: Path,
    binding_json: str,
    run_id: int,
    authorization_run_id: int,
    target_revision: str,
    candidate_digest: str,
    heads_before: tuple[str, ...],
    candidate_heads: tuple[str, ...],
    budget_seconds: int,
    maintenance_context: MaintenanceExecutionContext | None = None,
) -> int:
    path = _journal_path(deploy_directory)
    previous, previous_digest = _load_journal(path)
    binding = probe._binding_from_json(binding_json)
    maintenance_admission = (
        validate_maintenance_admission(maintenance_context, binding)
        if maintenance_context is not None
        else None
    )
    if previous is not None:
        ordinary_terminal = previous.phase in {
            MigrationPhase.COMPLETE,
            MigrationPhase.SAFE_PREDDL,
        }
        repair_terminal = (
            previous.phase
            in {
                MigrationPhase.FORWARD_REPAIR_REQUIRED,
                MigrationPhase.COMPLETE,
            }
            and previous.terminal is not None
            and not previous.terminal.oom_killed
            and previous.terminal.restart_count == 0
            and maintenance_admission is not None
            and previous.identity.invocation_id
            == maintenance_admission.failed_invocation_id
            and previous_digest == maintenance_admission.failed_journal_sha256
        )
        if not ordinary_terminal and not repair_terminal:
            raise MigrationRefused("migration_interlock_pending")
        if maintenance_admission is not None and not repair_terminal:
            raise MigrationRefused("maintenance_repair_admission_stale")
    elif maintenance_admission is not None:
        raise MigrationRefused("maintenance_repair_admission_stale")
    if (
        binding.candidate_revision != target_revision
        or binding.candidate_digest != candidate_digest
        or type(budget_seconds) is not int
        or not 1 <= budget_seconds <= 1200
        or any(
            _HEAD.fullmatch(item) is None for item in (*heads_before, *candidate_heads)
        )
    ):
        raise MigrationRefused("migration_input_unproved")
    if maintenance_admission is not None:
        if (
            maintenance_context is None
            or maintenance_context.deploy_directory != deploy_directory
            or maintenance_context.authorization_run_id != authorization_run_id
            or maintenance_context.deployment_run_id != run_id
            or maintenance_admission.target_revision != target_revision
            or maintenance_admission.target_digest != candidate_digest
            or maintenance_admission.target_image_id != binding.candidate_image_id
            or previous is None
            or tuple(sorted(set(heads_before))) != previous.candidate_heads
        ):
            raise MigrationRefused("maintenance_repair_admission_unproved")
        prior_binding = probe._binding_from_json(json.dumps(previous.network_binding))
        if (
            binding.network_id != prior_binding.network_id
            or binding.network_name != prior_binding.network_name
            or binding.app_container_id != prior_binding.app_container_id
            or binding.app_image_id != prior_binding.app_image_id
            or binding.app_digest != prior_binding.app_digest
            or binding.app_revision != prior_binding.app_revision
            or binding.app_started_at != prior_binding.app_started_at
            or binding.app_restart_count != prior_binding.app_restart_count
            or binding.app_extra_hosts != prior_binding.app_extra_hosts
            or binding.database != prior_binding.database
            or binding.broker != prior_binding.broker
            or binding.writers != prior_binding.writers
            or binding.inactive_services != prior_binding.inactive_services
            or binding.compose_arguments != prior_binding.compose_arguments
        ):
            raise MigrationRefused("maintenance_repair_binding_changed")
    verify_details = probe._verify(binding, allow_stopped_app=True)
    image = _image_details(binding)
    config = image.get("Config")
    labels = config.get("Labels") if type(config) is dict else None
    if type(labels) is not dict or labels.get(_LABEL_REVISION) != target_revision:
        raise MigrationRefused("migration_image_unproved")
    if previous is not None:
        _archive_terminal(path, previous)

    invocation_id = str(uuid.uuid4())
    name = f"dotmac_sub_migrate_{run_id}_{invocation_id.replace('-', '')[:16]}"
    identity = MigrationIdentity(
        run_id,
        authorization_run_id,
        target_revision,
        candidate_digest,
        binding.candidate_image_id,
        invocation_id,
        1,
        name,
        None,
    )
    record = MigrationInvocation(
        identity,
        MigrationPhase.CREATE_REQUESTED,
        tuple(sorted(set(heads_before))),
        tuple(sorted(set(candidate_heads))),
        binding.public_json(),
    )
    _write(path, record)
    database_url, broker_url, broker_key = verify_details
    command = [
        "docker",
        "create",
        "--name",
        name,
        "--read-only",
        "--network",
        binding.network_id,
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--entrypoint",
        "/opt/venv/bin/python",
        "--workdir",
        "/app",
        "--env-file",
        "/dev/stdin",
        "--label",
        f"{_LABEL_RUN}={run_id}",
        "--label",
        f"{_LABEL_AUTH}={authorization_run_id}",
        "--label",
        f"{_LABEL_INVOCATION}={invocation_id}",
        "--label",
        f"{_LABEL_REVISION}={target_revision}",
    ]
    hosts = {
        binding.database_hostname: binding.database.ipv4_address,
        binding.broker_hostname: binding.broker.ipv4_address,
    }
    for hostname, address in sorted(hosts.items()):
        command.extend(("--add-host", f"{hostname}:{address}"))
    command.extend(
        (binding.candidate_image_id, "-I", "-B", "-m", "alembic", "upgrade", "heads")
    )
    env_input = f"DATABASE_URL={database_url}\n{broker_key}={broker_url}\n"
    try:
        created = probe._run(command, input_text=env_input, side_effecting=True)
    except probe.ProbeIndeterminate:
        _write_phase(
            path, record, MigrationPhase.INDETERMINATE, terminal_detail="timed-out"
        )
        raise MigrationRefused("migration_create_indeterminate") from None
    if created.returncode != 0:
        _write_phase(path, record, MigrationPhase.INDETERMINATE)
        raise MigrationRefused("migration_create_unproved")
    identifier = created.stdout.strip()
    if _CONTAINER_ID.fullmatch(identifier) is None:
        _write_phase(path, record, MigrationPhase.INDETERMINATE)
        raise MigrationRefused("migration_create_unproved")
    identity = MigrationIdentity(**{**asdict(identity), "container_id": identifier})
    record = _write_phase(path, record, MigrationPhase.CREATED, identity=identity)
    _inspect_exact(
        identity, binding, expected_status="created", runtime_config=verify_details
    )
    record = _write_phase(path, record, MigrationPhase.START_REQUESTED)
    try:
        started = probe._run(
            ["docker", "start", identifier], timeout=30, side_effecting=True
        )
        start_acknowledged = started.returncode == 0
    except probe.ProbeIndeterminate:
        start_acknowledged = False
        record = _write_phase(
            path, record, MigrationPhase.INDETERMINATE, terminal_detail="timed-out"
        )
    if not start_acknowledged:
        # A failed/timed-out client response cannot prove that start did not
        # reach Docker. Continue only if the exact ID itself proves a known
        # running or exited state; never issue start again.
        try:
            value = _inspect(identity.container_id or "")
            state = value.get("State")
            status = state.get("Status") if type(state) is dict else None
            if status not in {"running", "exited"}:
                raise MigrationRefused("migration_start_indeterminate")
        except (MigrationRefused, OSError):
            raise MigrationRefused("migration_start_indeterminate") from None
    else:
        value = _inspect(identity.container_id or "")
        state_value = value.get("State")
        status_value = state_value.get("Status") if type(state_value) is dict else None
        if status_value not in {"running", "exited"}:
            raise MigrationRefused("migration_start_indeterminate")
        if status_value == "running":
            value = _inspect_exact(
                identity,
                binding,
                expected_status="running",
                runtime_config=verify_details,
            )
    state = value.get("State")
    if type(state) is dict and state.get("Status") == "running":
        record = _write_phase(path, record, MigrationPhase.RUNNING)
        deadline = time.monotonic() + budget_seconds
        while True:
            if time.monotonic() >= deadline:
                _write_phase(
                    path,
                    record,
                    MigrationPhase.INDETERMINATE,
                    terminal_detail="timed-out",
                )
                raise MigrationRefused("migration_runtime_indeterminate")
            time.sleep(min(2.0, max(0.0, deadline - time.monotonic())))
            value = _inspect(identity.container_id or "")
            state = value.get("State")
            if type(state) is not dict:
                raise MigrationRefused("migration_container_unproved")
            if state.get("Status") == "running":
                _inspect_exact(
                    identity,
                    binding,
                    expected_status="running",
                    runtime_config=verify_details,
                )
                continue
            if state.get("Status") != "exited":
                raise MigrationRefused("migration_runtime_indeterminate")
            break
    if type(state) is not dict or state.get("Status") != "exited":
        raise MigrationRefused("migration_runtime_indeterminate")
    value = _inspect_exact(
        identity, binding, expected_status="exited", runtime_config=verify_details
    )
    terminal = _terminal(identity, binding, value)
    probe._verify(binding, allow_stopped_app=True)
    record = _write_phase(
        path,
        record,
        MigrationPhase.EXITED,
        terminal=terminal,
        terminal_detail=None if terminal.exit_code == 0 else "exit-nonzero",
    )
    record = _write_phase(path, record, MigrationPhase.REMOVE_REQUESTED)
    try:
        removed = probe._run(["docker", "rm", identifier], side_effecting=True)
    except probe.ProbeIndeterminate:
        _write_phase(
            path,
            record,
            MigrationPhase.INDETERMINATE,
            terminal_detail="cleanup-unproved",
        )
        raise MigrationRefused("migration_cleanup_unproved") from None
    if removed.returncode != 0:
        _write_phase(
            path,
            record,
            MigrationPhase.INDETERMINATE,
            terminal_detail="cleanup-unproved",
        )
        raise MigrationRefused("migration_cleanup_unproved")
    record = _write_phase(path, record, MigrationPhase.REMOVED)
    disposition = _classify_terminal_state(binding, record, terminal)
    if disposition is MigrationDisposition.COMPLETE:
        _write_phase(path, record, MigrationPhase.COMPLETE)
        return 0
    if disposition is MigrationDisposition.SAFE_PREDDL:
        _write_phase(
            path,
            record,
            MigrationPhase.SAFE_PREDDL,
            terminal_detail="exit-nonzero",
        )
        return terminal.exit_code or 1
    if disposition is MigrationDisposition.FORWARD_REPAIR_REQUIRED:
        _write_phase(path, record, MigrationPhase.FORWARD_REPAIR_REQUIRED)
        raise MigrationRefused("migration_forward_repair_required")
    _write_phase(path, record, MigrationPhase.INDETERMINATE)
    raise MigrationRefused("migration_database_state_unproved")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    guard = commands.add_parser("require-safe-preddl")
    guard.add_argument("--deploy-directory", type=Path, required=True)
    guard.set_defaults(handler=lambda args: require_safe_preddl(args.deploy_directory))
    run = commands.add_parser("run")
    run.add_argument("--deploy-directory", type=Path, required=True)
    run.add_argument("--binding-json", required=True)
    run.add_argument("--run-id", type=int, required=True)
    run.add_argument("--authorization-run-id", type=int, required=True)
    run.add_argument("--target-revision", required=True)
    run.add_argument("--candidate-digest", required=True)
    run.add_argument("--heads-before", action="append", required=True)
    run.add_argument("--candidate-head", action="append", required=True)
    run.add_argument("--budget-seconds", type=int, default=1200)
    run.add_argument("--maintenance-context-fd", type=int)
    run.set_defaults(handler=None)
    maintenance = commands.add_parser("maintenance-context")
    maintenance.add_argument("--fd", type=int, default=_MAINTENANCE_CONTEXT_FD)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.command == "require-safe-preddl":
            args.handler(args)
            return 0
        if args.command == "maintenance-context":
            context = read_maintenance_context(args.fd)
            if context is None:
                raise MigrationRefused("maintenance_context_missing")
            binding, heads_before, candidate_heads = prepare_maintenance_context(
                context
            )
            print(
                json.dumps(
                    {
                        "schema_version": 1,
                        "binding": binding.public_json(),
                        "heads_before": heads_before,
                        "candidate_heads": candidate_heads,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            return 0
        maintenance_context = (
            read_maintenance_context(args.maintenance_context_fd)
            if args.maintenance_context_fd is not None
            else None
        )
        if args.maintenance_context_fd is not None and maintenance_context is None:
            raise MigrationRefused("maintenance_context_missing")
        return run_migration(
            deploy_directory=args.deploy_directory,
            binding_json=args.binding_json,
            run_id=args.run_id,
            authorization_run_id=args.authorization_run_id,
            target_revision=args.target_revision,
            candidate_digest=args.candidate_digest,
            heads_before=tuple(sorted(set(args.heads_before))),
            candidate_heads=tuple(sorted(set(args.candidate_head))),
            budget_seconds=args.budget_seconds,
            maintenance_context=maintenance_context,
        )
    except (MigrationRefused, probe.ProbeRefused, OSError, ValueError):
        print(
            "PRODUCTION MIGRATION INDETERMINATE; RETAIN JOURNAL AND REPAIR FORWARD",
            file=sys.stderr,
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
