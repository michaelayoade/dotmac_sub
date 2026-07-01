"""Controlled operations for background worker services."""

from __future__ import annotations

import os
import re
import shlex
import subprocess  # nosec - command is built from a validated template/target.
from dataclasses import dataclass

from app.services import infrastructure_health

_TARGET_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_DEFAULT_RESTART_COMMAND = "docker compose restart {target}"


@dataclass(frozen=True)
class WorkerRestartResult:
    target: str
    ok: bool
    message: str
    returncode: int | None = None


def restart_enabled() -> bool:
    return infrastructure_health._celery_worker_restart_enabled()


def allowed_restart_targets() -> set[str]:
    return set(infrastructure_health._celery_queue_restart_targets().values())


def _restart_command(target: str) -> list[str]:
    template = os.getenv("CELERY_WORKER_RESTART_COMMAND", _DEFAULT_RESTART_COMMAND)
    command = template.format(target=target)
    return shlex.split(command)


def restart_worker_target(target: str) -> WorkerRestartResult:
    target = target.strip()
    if not restart_enabled():
        return WorkerRestartResult(
            target=target,
            ok=False,
            message="Worker restart is disabled by configuration.",
        )
    if not _TARGET_RE.match(target):
        return WorkerRestartResult(
            target=target,
            ok=False,
            message="Invalid worker restart target.",
        )
    if target not in allowed_restart_targets():
        return WorkerRestartResult(
            target=target,
            ok=False,
            message="Worker restart target is not allowed.",
        )

    try:
        command = _restart_command(target)
    except (KeyError, ValueError):
        return WorkerRestartResult(
            target=target,
            ok=False,
            message="Worker restart command template is invalid.",
        )
    if any("{target}" in part for part in command):
        # The template.format() call above should always replace this. Keep this
        # guard close to subprocess execution in case the template is malformed.
        return WorkerRestartResult(
            target=target,
            ok=False,
            message="Worker restart command template is invalid.",
        )
    try:
        result = subprocess.run(  # noqa: S603
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=float(os.getenv("CELERY_WORKER_RESTART_TIMEOUT_SECONDS", "20")),
        )
    except FileNotFoundError as exc:
        return WorkerRestartResult(
            target=target,
            ok=False,
            message=f"Restart command is unavailable: {exc.filename}",
        )
    except subprocess.TimeoutExpired:
        return WorkerRestartResult(
            target=target,
            ok=False,
            message="Restart command timed out.",
        )
    except OSError as exc:
        return WorkerRestartResult(
            target=target,
            ok=False,
            message=f"Restart command failed: {exc}",
        )

    if result.returncode == 0:
        return WorkerRestartResult(
            target=target,
            ok=True,
            message=f"Restart requested for {target}.",
            returncode=result.returncode,
        )

    detail = (result.stderr or result.stdout or "Restart command failed.").strip()
    return WorkerRestartResult(
        target=target,
        ok=False,
        message=detail[:300],
        returncode=result.returncode,
    )
