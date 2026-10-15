"""Hold trusted production admission through the exact staged deploy process."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
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

from scripts.production_trusted_rollback_preflight import (
    PreflightRefused,
    TrustedPreflightInput,
    TrustedPreflightOutcome,
    run_preflight,
)
from scripts.release_artifact_contract import (
    GitCommitSha,
    OCIImageDigest,
    WorkflowRunId,
)

_CANONICAL_LOCK: Final = Path("/var/lock/dotmac_sub_deploy.lock")
_LOCK_FD: Final = 8
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
    command: list[str], *, environment: dict[str, str], cwd: Path
) -> int:
    child = subprocess.Popen(
        command,
        cwd=cwd,
        env=environment,
        close_fds=True,
        pass_fds=(_LOCK_FD,),
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
        preflight = run_preflight(preflight_input)
        # Repeat the live-image proof immediately before staged code executes.
        # Staged code cannot be the sole authority for the transition.
        from scripts.production_trusted_rollback_preflight import _inspect_running_image

        if preflight.running_image is not None and (
            _inspect_running_image() != preflight.running_image
        ):
            raise TrustedDeploymentRefusal("running_image_changed_after_preflight")
        with tempfile.TemporaryDirectory(
            prefix="dotmac-trusted-deploy-"
        ) as private_directory:
            private_lock = Path(private_directory) / "legacy-deploy.lock"
            lock_descriptor = os.open(private_lock, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(lock_descriptor)
            child_environment = os.environ.copy()
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
            exit_code = _wait_for_child(
                command,
                environment=child_environment,
                cwd=inputs.staged_repository,
            )
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
    except (PreflightRefused, TrustedDeploymentRefusal, OSError, ValueError):
        print("TRUSTED PRODUCTION DEPLOY REFUSED", file=sys.stderr)
        return 2
    return outcome.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
