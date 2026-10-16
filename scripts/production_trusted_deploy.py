"""Hold trusted production admission through the exact staged deploy process."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Final

from scripts import production_deploy_probe as deploy_probe
from scripts.production_deploy_migration import (
    MaintenanceExecutionContext,
    MigrationDisposition,
    MigrationPhase,
    MigrationReconciliationRequest,
    MigrationRefused,
    prepare_maintenance_context,
    reconcile_pending,
)
from scripts.production_deploy_migration import (
    _load_journal as _load_migration_journal,
)
from scripts.production_deploy_migration import (
    guard_interlock as guard_migration_interlock,
)
from scripts.production_trusted_rollback_preflight import (
    PreflightRefused,
    SourceTransition,
    TrustedPreflightInput,
    TrustedPreflightOutcome,
    _classify_transition,
    _verify_authorization,
    run_preflight,
)
from scripts.release_artifact_contract import (
    GitCommitSha,
    OCIImageDigest,
    WorkflowRunId,
)

_CANONICAL_LOCK: Final = Path("/var/lock/dotmac_sub_deploy.lock")
_LOCK_FD: Final = 8
_MAINTENANCE_FD: Final = 7
_LOCK_ABI_SHA256: Final = (
    "10d40afcc2bbb16c7210c2f87c55a191de0b2053101f5d451aa5712290dad079"
)
_LOCK_ABI_START: Final = (
    'LOCK_FILE="${DEPLOY_LOCK_FILE:-/var/lock/dotmac_sub_deploy.lock}"'
)
_LOCK_ABI_END: Final = "\n# A pg_dump left behind"
_CHILD_WAIT_AFTER_SIGNAL_SECONDS: Final = 1200


class TrustedDeploymentRefusal(ValueError):
    """Closed denial emitted without command output or credential values."""


@dataclass(frozen=True, slots=True)
class DeploymentOptions:
    rollback_authorization_path: Path | None = None
    bootstrap_authorization_path: Path | None = None
    hotfix_change_reference: str | None = None
    hotfix_reason: str | None = None
    resume_failed_run_id: int | None = None
    resume_backup_path: str | None = None

    def __post_init__(self) -> None:
        if (self.hotfix_change_reference is None) != (self.hotfix_reason is None):
            raise ValueError("incomplete hotfix attribution")
        if (
            self.hotfix_change_reference is not None
            and not self.hotfix_change_reference.strip()
        ):
            raise ValueError("empty hotfix reference")
        if self.hotfix_reason is not None and not self.hotfix_reason.strip():
            raise ValueError("empty hotfix reason")
        if (self.resume_failed_run_id is None) != (self.resume_backup_path is None):
            raise ValueError("incomplete post-migration resume attribution")
        if self.resume_failed_run_id is not None and self.resume_failed_run_id <= 0:
            raise ValueError("invalid failed run id")
        if self.bootstrap_authorization_path is not None and (
            self.rollback_authorization_path is not None
            or self.hotfix_change_reference is not None
            or self.resume_failed_run_id is not None
        ):
            raise ValueError("bootstrap cannot combine with another special mode")
        if (
            self.hotfix_change_reference is not None
            and self.resume_failed_run_id is not None
        ):
            raise ValueError("hotfix and resume modes cannot be combined")


@dataclass(frozen=True, slots=True)
class TrustedDeploymentInput:
    preflight: TrustedPreflightInput
    staged_repository: Path
    options: DeploymentOptions


@dataclass(frozen=True, slots=True)
class TrustedDeploymentOutcome:
    exit_code: int
    preflight: TrustedPreflightOutcome


def _deployment_run_id_from_environment() -> int:
    raw = os.environ.get("DEPLOY_RUN_ID", "")
    if not raw.isascii() or not raw.isdecimal() or raw.startswith("0"):
        raise TrustedDeploymentRefusal("deployment_run_id_unproved")
    try:
        return WorkflowRunId(int(raw)).value
    except (ValueError, OverflowError):
        raise TrustedDeploymentRefusal("deployment_run_id_unproved") from None


def _require_clean_target(
    repository: Path, expected: GitCommitSha
) -> tuple[Path, Path]:
    if not repository.is_absolute() or not repository.is_dir():
        raise TrustedDeploymentRefusal("staged_source_unproved")
    try:
        revision = subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise TrustedDeploymentRefusal("staged_source_unproved") from None
    if revision.returncode != 0 or revision.stdout.strip() != expected.value:
        raise TrustedDeploymentRefusal("staged_source_unproved")
    _require_clean_source_tree(repository, refusal="staged_source_unproved")
    scripts_directory = repository / "scripts"
    try:
        scripts_info = scripts_directory.lstat()
        app_info = (repository / "app").lstat()
    except OSError:
        raise TrustedDeploymentRefusal("staged_source_unproved") from None
    if not stat.S_ISDIR(scripts_info.st_mode) or not stat.S_ISDIR(app_info.st_mode):
        raise TrustedDeploymentRefusal("staged_source_unproved")
    wrapper = repository / "scripts" / "deploy_production.sh"
    deploy = repository / "scripts" / "deploy.sh"
    for path in (wrapper, deploy):
        try:
            info = path.lstat()
        except OSError:
            raise TrustedDeploymentRefusal("staged_source_unproved") from None
        if not stat.S_ISREG(info.st_mode):
            raise TrustedDeploymentRefusal("staged_source_unproved")
    try:
        source = deploy.read_text(encoding="utf-8")
        start = source.index(_LOCK_ABI_START)
        end = source.index(_LOCK_ABI_END, start)
    except (OSError, UnicodeError, ValueError):
        raise TrustedDeploymentRefusal("unsupported_deploy_lock_abi") from None
    if (
        hashlib.sha256(source[start:end].encode("utf-8")).hexdigest()
        != _LOCK_ABI_SHA256
    ):
        raise TrustedDeploymentRefusal("unsupported_deploy_lock_abi")
    return wrapper, deploy


def _require_clean_source_tree(repository: Path, *, refusal: str) -> None:
    try:
        diff = subprocess.run(
            ["git", "-C", str(repository), "diff", "--quiet", "HEAD"],
            check=False,
            capture_output=True,
            timeout=15,
        )
        untracked = subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "ls-files",
                "--others",
                "--exclude-standard",
                "-z",
            ],
            check=False,
            capture_output=True,
            timeout=15,
        )
        ignored_source = subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "ls-files",
                "--others",
                "--ignored",
                "--exclude-standard",
                "-z",
                "--",
                "app",
                "scripts",
                ":(top,glob)*.py",
                ":(top,glob)*.pyc",
                ":(top,glob)*.pyo",
                ":(top,glob)*.so",
                ":(top,glob)*.pth",
                ":(top,glob)*.sh",
            ],
            check=False,
            capture_output=True,
            timeout=15,
        )
        modes = subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "ls-files",
                "--stage",
                "-z",
                "--",
                "app",
                "scripts",
            ],
            check=False,
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise TrustedDeploymentRefusal(refusal) from None
    if (
        diff.returncode != 0
        or untracked.returncode != 0
        or ignored_source.returncode != 0
        or modes.returncode != 0
    ):
        raise TrustedDeploymentRefusal(refusal)
    source_suffixes = {".py", ".pyc", ".pyo", ".so", ".pyd", ".pth", ".sh"}
    try:
        untracked_paths = [
            Path(value.decode("utf-8", errors="strict"))
            for value in untracked.stdout.split(b"\0")
            if value
        ]
        untracked_paths.extend(
            Path(value.decode("utf-8", errors="strict"))
            for value in ignored_source.stdout.split(b"\0")
            if value
        )
    except UnicodeDecodeError:
        raise TrustedDeploymentRefusal(refusal) from None
    for path in untracked_paths:
        if path.suffix in source_suffixes and (
            len(path.parts) == 1 or path.parts[0] in {"app", "scripts"}
        ):
            raise TrustedDeploymentRefusal(refusal)
        try:
            mode = (repository / path).stat().st_mode
        except OSError:
            raise TrustedDeploymentRefusal(refusal) from None
        if mode & 0o111 and path.parts[0] in {"app", "scripts"}:
            raise TrustedDeploymentRefusal(refusal)
    for entry in modes.stdout.split(b"\0"):
        if not entry:
            continue
        metadata, _separator, _path = entry.partition(b"\t")
        if metadata.split(b" ", 1)[0] == b"120000":
            raise TrustedDeploymentRefusal(refusal)


def _require_trusted_control(repository: Path, expected: GitCommitSha) -> None:
    if (
        not repository.is_absolute()
        or not repository.is_dir()
        or Path(__file__).resolve().parent.parent != repository.resolve()
    ):
        raise TrustedDeploymentRefusal("trusted_control_unproved")
    try:
        result = subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise TrustedDeploymentRefusal("trusted_control_unproved") from None
    if result.returncode != 0 or result.stdout.strip() != expected.value:
        raise TrustedDeploymentRefusal("trusted_control_unproved")
    _require_clean_source_tree(repository, refusal="trusted_control_unproved")


def _acquire_canonical_lock() -> int:
    if "DEPLOY_LOCK_FILE" in os.environ:
        raise TrustedDeploymentRefusal("canonical_lock_override_refused")
    try:
        descriptor = os.open(
            _CANONICAL_LOCK,
            os.O_CREAT
            | os.O_RDWR
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise TrustedDeploymentRefusal("canonical_lock_unavailable")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if descriptor != _LOCK_FD:
            try:
                os.fstat(_LOCK_FD)
            except OSError:
                os.dup2(descriptor, _LOCK_FD, inheritable=True)
                os.close(descriptor)
                descriptor = _LOCK_FD
            else:
                os.close(descriptor)
                raise TrustedDeploymentRefusal("canonical_lock_descriptor_unavailable")
        os.set_inheritable(descriptor, True)
        return descriptor
    except TrustedDeploymentRefusal:
        raise
    except OSError:
        raise TrustedDeploymentRefusal("canonical_lock_unavailable") from None


def _wrapper_arguments(
    wrapper: Path, inputs: TrustedPreflightInput, options: DeploymentOptions
) -> list[str]:
    args = [
        "bash",
        str(wrapper),
        inputs.target_image_digest.value,
        str(inputs.production_authorization_path),
    ]
    if options.rollback_authorization_path is not None:
        args.extend(
            ("--rollback-authorization", str(options.rollback_authorization_path))
        )
    if options.bootstrap_authorization_path is not None:
        args.extend(
            ("--bootstrap-authorization", str(options.bootstrap_authorization_path))
        )
    if options.hotfix_change_reference is not None:
        args.extend(
            (
                "--hotfix-no-migrations",
                "--change-reference",
                options.hotfix_change_reference,
                "--reason",
                options.hotfix_reason or "",
            )
        )
    if options.resume_failed_run_id is not None:
        args.extend(
            (
                "--resume-after-migration",
                "--failed-run-id",
                str(options.resume_failed_run_id),
                "--backup-path",
                options.resume_backup_path or "",
            )
        )
    return args


def _wait_for_child(
    command: list[str],
    *,
    environment: dict[str, str],
    cwd: Path,
    extra_pass_fds: tuple[int, ...] = (),
) -> int:
    child = subprocess.Popen(
        command,
        cwd=cwd,
        env=environment,
        close_fds=True,
        pass_fds=(_LOCK_FD, *extra_pass_fds),
        start_new_session=True,
    )
    pending_signal: list[int] = []

    def forward(signum: int, _frame: FrameType | None) -> None:
        if pending_signal:
            return
        pending_signal.append(signum)
        try:
            os.killpg(child.pid, signum)
        except ProcessLookupError:
            pass

    old_handlers = {
        signum: signal.signal(signum, forward)
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    }
    try:
        signaled_at: float | None = None
        while child.poll() is None:
            if pending_signal and signaled_at is None:
                signaled_at = time.monotonic()
            if (
                signaled_at is not None
                and time.monotonic() - signaled_at >= _CHILD_WAIT_AFTER_SIGNAL_SECONDS
            ):
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                break
            time.sleep(0.1)
        return_code = child.wait()
        group_deadline = time.monotonic() + _CHILD_WAIT_AFTER_SIGNAL_SECONDS
        while True:
            try:
                os.killpg(child.pid, 0)
            except ProcessLookupError:
                break
            if time.monotonic() >= group_deadline:
                return_code = return_code or 1
                break
            time.sleep(0.1)
        if pending_signal:
            return return_code if return_code != 0 else 128 + pending_signal[0]
        return return_code
    finally:
        for signum, previous in old_handlers.items():
            signal.signal(signum, previous)


def _app_container_is_running() -> bool:
    try:
        app = deploy_probe._inspect_named("dotmac_sub_app")
        state = app.get("State")
        if type(state) is not dict:
            raise TrustedDeploymentRefusal("incumbent_state_unproved")
        return state.get("Running") is True and state.get("Status") == "running"
    except deploy_probe.ProbeRefused:
        return False


def _maintenance_repair_context(
    inputs: TrustedDeploymentInput,
) -> tuple[MaintenanceExecutionContext, SourceTransition]:
    preflight_input = inputs.preflight
    if (
        inputs.options.rollback_authorization_path is not None
        or inputs.options.bootstrap_authorization_path is not None
        or inputs.options.hotfix_change_reference is not None
        or inputs.options.resume_failed_run_id is not None
    ):
        raise TrustedDeploymentRefusal("maintenance_options_conflict")
    _verify_authorization(preflight_input)
    record, digest = _load_migration_journal(
        preflight_input.deploy_directory / ".deployment-state" / "migration.json"
    )
    if (
        record is None
        or digest is None
        or record.phase
        not in {
            MigrationPhase.FORWARD_REPAIR_REQUIRED,
            MigrationPhase.COMPLETE,
        }
        or record.terminal is None
        or record.terminal.oom_killed
        or record.terminal.restart_count != 0
    ):
        raise TrustedDeploymentRefusal("maintenance_journal_unproved")
    transition = _classify_transition(
        GitCommitSha(record.identity.target_revision),
        preflight_input.target_revision,
        repository=preflight_input.control_repository,
    )
    if transition not in {SourceTransition.SAME, SourceTransition.FORWARD} or (
        transition is SourceTransition.SAME
        and record.identity.candidate_digest
        != preflight_input.target_image_digest.value
    ):
        raise TrustedDeploymentRefusal("maintenance_source_transition_unproved")
    if record.phase is MigrationPhase.COMPLETE and _app_container_is_running():
        raise TrustedDeploymentRefusal("maintenance_not_required_for_running_release")
    target_id, target_digest, _environment = deploy_probe._candidate(
        f"{deploy_probe._REPOSITORY}@{preflight_input.target_image_digest.value}",
        preflight_input.target_revision.value,
    )
    deployment_run_id = _deployment_run_id_from_environment()
    context = MaintenanceExecutionContext(
        deploy_directory=preflight_input.deploy_directory,
        control_repository=preflight_input.control_repository.resolve(),
        authorization_main_revision=(preflight_input.authorization_main_revision.value),
        production_authorization_path=(
            preflight_input.production_authorization_path.resolve()
        ),
        authorization_run_id=preflight_input.authorization_run_id.value,
        target_revision=preflight_input.target_revision.value,
        target_digest=target_digest,
        target_image_id=target_id,
        failed_invocation_id=record.identity.invocation_id,
        failed_journal_sha256=digest,
        deployment_run_id=deployment_run_id,
    )
    prepare_maintenance_context(context)
    return context, transition


def _maintenance_context_fd(context: MaintenanceExecutionContext) -> int:
    try:
        os.fstat(_MAINTENANCE_FD)
    except OSError:
        pass
    else:
        raise TrustedDeploymentRefusal("maintenance_context_fd_collision")
    path: str | None = None
    writable_fd: int | None = None
    readonly_fd: int | None = None
    try:
        writable_fd, path = tempfile.mkstemp(prefix="dotmac-maintenance-context-")
        os.fchmod(writable_fd, 0o600)
        payload = (
            json.dumps(
                context.public_json(),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode("ascii")
            + b"\n"
        )
        if len(payload) > 65_536:
            raise TrustedDeploymentRefusal("maintenance_context_unproved")
        with os.fdopen(writable_fd, "wb", closefd=True) as stream:
            writable_fd = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        readonly_fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        os.unlink(path)
        path = None
        if readonly_fd != _MAINTENANCE_FD:
            os.dup2(readonly_fd, _MAINTENANCE_FD, inheritable=True)
            os.close(readonly_fd)
        else:
            os.set_inheritable(readonly_fd, True)
        return _MAINTENANCE_FD
    except TrustedDeploymentRefusal:
        raise
    except OSError:
        raise TrustedDeploymentRefusal("maintenance_context_write_failed") from None
    finally:
        if writable_fd is not None:
            os.close(writable_fd)
        if readonly_fd is not None and readonly_fd != _MAINTENANCE_FD:
            try:
                os.close(readonly_fd)
            except OSError:
                pass
        if path is not None:
            try:
                os.unlink(path)
            except OSError:
                pass


def run_trusted_deployment(inputs: TrustedDeploymentInput) -> TrustedDeploymentOutcome:
    lock_fd = _acquire_canonical_lock()
    try:
        preflight_input = inputs.preflight
        _require_trusted_control(
            preflight_input.control_repository,
            preflight_input.authorization_main_revision,
        )
        if (
            not preflight_input.deploy_directory.is_absolute()
            or preflight_input.deploy_directory.resolve()
            != preflight_input.deploy_directory
            or not (preflight_input.deploy_directory / ".env").is_file()
        ):
            raise TrustedDeploymentRefusal("deploy_directory_unproved")
        wrapper, _deploy = _require_clean_target(
            inputs.staged_repository, preflight_input.target_revision
        )
        journal_path = (
            preflight_input.deploy_directory / ".deployment-state" / "migration.json"
        )
        journal, journal_digest = _load_migration_journal(journal_path)
        if journal is not None and journal.phase not in {
            MigrationPhase.COMPLETE,
            MigrationPhase.SAFE_PREDDL,
            MigrationPhase.FORWARD_REPAIR_REQUIRED,
        }:
            if journal_digest is None:
                raise TrustedDeploymentRefusal("migration_journal_unproved")
            reconciled = reconcile_pending(
                MigrationReconciliationRequest(
                    preflight_input.deploy_directory,
                    journal.identity.invocation_id,
                    journal_digest,
                )
            )
            if reconciled.disposition is MigrationDisposition.UNRESOLVED:
                raise TrustedDeploymentRefusal("migration_reconciliation_unresolved")
            journal, journal_digest = _load_migration_journal(journal_path)

        maintenance: MaintenanceExecutionContext | None = None
        if (
            journal is not None
            and journal.phase is MigrationPhase.FORWARD_REPAIR_REQUIRED
        ):
            maintenance, transition = _maintenance_repair_context(inputs)
            preflight = TrustedPreflightOutcome(transition, None, True)
        elif (
            journal is not None
            and journal.phase is MigrationPhase.COMPLETE
            and not _app_container_is_running()
        ):
            maintenance, transition = _maintenance_repair_context(inputs)
            preflight = TrustedPreflightOutcome(transition, None, True)
        else:
            # A pending safe-pre-DDL record is admitted only by its normal
            # exact-incumbent path; no stopped-app exemption is granted here.
            guard_migration_interlock(preflight_input.deploy_directory)
            preflight = run_preflight(preflight_input)
        # Repeat the live-image proof immediately before staged code executes.
        # Staged code cannot be the sole authority for the transition.
        from scripts.production_trusted_rollback_preflight import _inspect_running_image

        if (
            maintenance is None
            and preflight.running_image is not None
            and (_inspect_running_image() != preflight.running_image)
        ):
            raise TrustedDeploymentRefusal("running_image_changed_after_preflight")
        with tempfile.TemporaryDirectory(
            prefix="dotmac-trusted-deploy-"
        ) as private_directory:
            private_lock = Path(private_directory) / "legacy-deploy.lock"
            lock_descriptor = os.open(private_lock, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(lock_descriptor)
            child_environment = os.environ.copy()
            child_environment.pop("DOTMAC_MAINTENANCE_CONTEXT_FD", None)
            child_environment.update(
                {
                    "AUTHORIZATION_RUN_ID": str(
                        preflight_input.authorization_run_id.value
                    ),
                    "DEPLOY_LOCK_FILE": str(private_lock),
                    "DEPLOY_DIR": str(preflight_input.deploy_directory),
                    "REPO_DIR": str(inputs.staged_repository),
                }
            )
            command = _wrapper_arguments(wrapper, preflight_input, inputs.options)
            maintenance_fd = None
            try:
                pass_fds: tuple[int, ...] = ()
                if maintenance is not None:
                    maintenance_fd = _maintenance_context_fd(maintenance)
                    child_environment["DOTMAC_MAINTENANCE_CONTEXT_FD"] = str(
                        _MAINTENANCE_FD
                    )
                    child_environment["DEPLOY_RUN_ID"] = str(
                        maintenance.deployment_run_id
                    )
                    pass_fds = (_MAINTENANCE_FD,)
                exit_code = _wait_for_child(
                    command,
                    environment=child_environment,
                    cwd=inputs.staged_repository,
                    extra_pass_fds=pass_fds,
                )
            finally:
                if maintenance_fd is not None:
                    os.close(maintenance_fd)
            return TrustedDeploymentOutcome(exit_code, preflight)
    finally:
        try:
            os.close(lock_fd)
        except OSError:
            pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--production-authorization", type=Path, required=True)
    parser.add_argument("--authorization-run-id", type=int, required=True)
    parser.add_argument("--authorization-main-revision", required=True)
    parser.add_argument("--target-revision", required=True)
    parser.add_argument("--target-image-digest", required=True)
    parser.add_argument("--deploy-directory", type=Path, required=True)
    parser.add_argument("--staged-repository", type=Path, required=True)
    parser.add_argument("--control-repository", type=Path, required=True)
    parser.add_argument("--rollback-authorization", type=Path)
    parser.add_argument("--bootstrap-authorization", type=Path)
    parser.add_argument("--hotfix-change-reference")
    parser.add_argument("--hotfix-reason")
    parser.add_argument("--resume-failed-run-id", type=int)
    parser.add_argument("--resume-backup-path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        preflight = TrustedPreflightInput(
            production_authorization_path=args.production_authorization,
            authorization_run_id=WorkflowRunId(args.authorization_run_id),
            authorization_main_revision=GitCommitSha(args.authorization_main_revision),
            target_revision=GitCommitSha(args.target_revision),
            target_image_digest=OCIImageDigest(args.target_image_digest),
            deploy_directory=args.deploy_directory,
            rollback_authorization_path=args.rollback_authorization,
            bootstrap_authorization_path=args.bootstrap_authorization,
            control_repository=args.control_repository,
        )
        options = DeploymentOptions(
            rollback_authorization_path=args.rollback_authorization,
            bootstrap_authorization_path=args.bootstrap_authorization,
            hotfix_change_reference=args.hotfix_change_reference,
            hotfix_reason=args.hotfix_reason,
            resume_failed_run_id=args.resume_failed_run_id,
            resume_backup_path=args.resume_backup_path,
        )
        outcome = run_trusted_deployment(
            TrustedDeploymentInput(preflight, args.staged_repository, options)
        )
    except (
        MigrationRefused,
        PreflightRefused,
        TrustedDeploymentRefusal,
        OSError,
        ValueError,
    ):
        print("TRUSTED PRODUCTION DEPLOY REFUSED", file=sys.stderr)
        return 2
    return outcome.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
