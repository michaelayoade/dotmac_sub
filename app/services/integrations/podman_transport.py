"""Rootless-Podman transport for the external connector runtime.

Phase 3 of ADR 0004, transport half. Implements ``RunnerTransport`` by running
the connector's digest-pinned image as a short-lived, hardened, rootless Podman
container: the ``RunnerRequest`` JSON goes in on stdin, the ``RunnerResponse``
JSON comes back on stdout, the container is killed at the deadline and removed.

Security posture (ADR 0004 resolved decision 1). Every container runs
``--read-only`` with ``--cap-drop=ALL``, ``--security-opt=no-new-privileges``,
no host mounts, and bounded memory, CPU, and pids. Rootless execution maps
container-root to an unprivileged host uid, so an escape lands unprivileged.
Secret material is written to a tmpfs env file readable only by this user,
passed with ``--env-file`` (never on argv, where ``ps`` would expose it), and
deleted in a ``finally``. Egress restriction is deliberately deferred to Phase
4; ``network`` is a parameter so that phase can tighten it without touching
callers.

``_build_argv`` is a pure function so the exact command — the security flags in
particular — is unit-tested without invoking Podman. The live exchange is
covered by an integration test that runs a real example connector on a host
where Podman is present.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from app.services.integrations.external_runner import (
    RunnerTimeout,
    RunnerTransportError,
)
from app.services.integrations.runner_protocol import RunnerRequest, RunnerResponse

# Connectors read each secret binding from an env var under this prefix. Config
# is not here — it travels in the request on stdin. Only secrets go by env, so
# they never share a channel with replayable request data.
SECRET_ENV_PREFIX = "DM_SECRET_"

_DEFAULT_MEMORY = "256m"
_DEFAULT_PIDS_LIMIT = 128
# CPU limiting is opt-in, not defaulted. `--cpus` needs the cpu cgroup
# controller delegated to the rootless user, which is not the default on
# Ubuntu 22.04 (only memory and pids are delegated out of the box). Applying it
# where the controller is absent makes every operation fail, so the transport
# only sets it when a deployment explicitly opts in on a host with delegation
# configured (see the Phase 3 deployment note in ADR 0004). memory and pids —
# the OOM and fork-bomb controls — work rootless everywhere and stay on.
_DEFAULT_CPUS: str | None = None
_MIN_DEADLINE_SECONDS = 1
# How much longer than the authoritative app-side deadline Podman's own
# --timeout runs, so conmon reaps a container orphaned by killing podman run.
_CONTAINER_REAP_GRACE_SECONDS = 5


def _secret_env_name(binding: str) -> str:
    return SECRET_ENV_PREFIX + binding.upper()


def _build_argv(
    image_ref: str,
    *,
    deadline_seconds: int,
    env_file: str,
    network: str | None = None,
    memory: str = _DEFAULT_MEMORY,
    cpus: str | None = _DEFAULT_CPUS,
    pids_limit: int = _DEFAULT_PIDS_LIMIT,
) -> list[str]:
    """Build the hardened ``podman run`` argv for one operation.

    Pure and side-effect free so the security flags are asserted in isolation.
    Carries no secret value: credentials arrive through ``env_file``, never as
    an argument.
    """

    if deadline_seconds < _MIN_DEADLINE_SECONDS:
        deadline_seconds = _MIN_DEADLINE_SECONDS
    argv = [
        "podman",
        "run",
        "--rm",
        "--interactive",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        # A read-only rootfs still needs a scratch area; keep it small, noexec,
        # and in memory rather than granting a writable layer.
        "--tmpfs=/tmp:rw,noexec,nosuid,size=16m",
        f"--memory={memory}",
        f"--pids-limit={pids_limit}",
        # conmon kills the container this many seconds after it starts; the
        # subprocess deadline below is the authoritative one.
        f"--timeout={deadline_seconds}",
        f"--env-file={env_file}",
    ]
    if cpus is not None:
        argv.append(f"--cpus={cpus}")
    if network is not None:
        argv.append(f"--network={network}")
    argv.append(image_ref)
    return argv


class PodmanTransport:
    """Carry one operation to a connector container over rootless Podman."""

    def __init__(
        self,
        *,
        network: str | None = None,
        memory: str = _DEFAULT_MEMORY,
        cpus: str | None = _DEFAULT_CPUS,
        pids_limit: int = _DEFAULT_PIDS_LIMIT,
        runtime_dir: str | None = None,
        podman_path: str = "podman",
    ) -> None:
        self._network = network
        self._memory = memory
        self._cpus = cpus
        self._pids_limit = pids_limit
        # Secret env files live on tmpfs. XDG_RUNTIME_DIR is tmpfs and
        # user-private on a rootless host; fall back only if it is unset.
        self._runtime_dir = runtime_dir or os.environ.get("XDG_RUNTIME_DIR")
        self._podman_path = podman_path

    def _write_secret_env_file(self, secret_material: Mapping[str, str]) -> str:
        fd, path = tempfile.mkstemp(
            prefix="dm-runner-secret-", suffix=".env", dir=self._runtime_dir
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                for name, value in secret_material.items():
                    # env-file values are taken verbatim to end of line; a
                    # newline in a credential would corrupt the next entry.
                    if "\n" in value or "\r" in value:
                        raise RunnerTransportError(
                            f"secret {name!r} contains a newline and cannot be "
                            "delivered as an environment value"
                        )
                    handle.write(f"{_secret_env_name(name)}={value}\n")
        except Exception:
            Path(path).unlink(missing_ok=True)
            raise
        return path

    def _deadline_seconds(self, deadline_at: datetime) -> int:
        remaining = (deadline_at - datetime.now(UTC)).total_seconds()
        return max(_MIN_DEADLINE_SECONDS, int(remaining))

    def _parse_response(
        self, stdout: bytes, *, request: RunnerRequest
    ) -> RunnerResponse:
        text = stdout.decode("utf-8", errors="replace").strip()
        if not text:
            raise RunnerTransportError("connector produced no response on stdout")
        # A connector may log to stdout before the response; take the last line.
        last = text.splitlines()[-1]
        try:
            return RunnerResponse.model_validate_json(last)
        except ValidationError as exc:
            raise RunnerTransportError(
                f"connector response did not match the wire contract: "
                f"{exc.error_count()} error(s)"
            ) from exc

    def exchange(
        self,
        *,
        request: RunnerRequest,
        image_ref: str,
        secret_material: Mapping[str, str],
        deadline_at: datetime,
    ) -> RunnerResponse:
        deadline_seconds = self._deadline_seconds(deadline_at)
        env_file = self._write_secret_env_file(secret_material)
        # The subprocess deadline is authoritative: it raises TimeoutExpired
        # unambiguously, whereas Podman's own deadline kill exits 255 —
        # indistinguishable from a generic error. Podman's --timeout is set
        # longer, purely to reap the container if the podman process we killed
        # left conmon holding it.
        argv = self._argv(
            image_ref, deadline_seconds + _CONTAINER_REAP_GRACE_SECONDS, env_file
        )
        try:
            completed = subprocess.run(
                argv,
                input=request.model_dump_json().encode("utf-8"),
                capture_output=True,
                timeout=deadline_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RunnerTimeout(
                f"connector {image_ref} exceeded its {deadline_seconds}s deadline"
            ) from exc
        except FileNotFoundError as exc:
            raise RunnerTransportError(
                f"container runtime {self._podman_path!r} is not available"
            ) from exc
        finally:
            Path(env_file).unlink(missing_ok=True)

        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            # If Podman's backstop deadline fired first in a race, treat it as a
            # timeout so execute maps it to reconciliation, not a plain retry.
            if completed.returncode in (124, 137) or "timed out" in detail.lower():
                raise RunnerTimeout(f"connector {image_ref} was killed at its deadline")
            raise RunnerTransportError(
                f"connector {image_ref} exited {completed.returncode}: {detail[:500]}"
            )
        return self._parse_response(completed.stdout, request=request)

    def _argv(
        self, image_ref: str, deadline_seconds: int, env_file: str
    ) -> Sequence[str]:
        argv = _build_argv(
            image_ref,
            deadline_seconds=deadline_seconds,
            env_file=env_file,
            network=self._network,
            memory=self._memory,
            cpus=self._cpus,
            pids_limit=self._pids_limit,
        )
        if self._podman_path != "podman":
            argv[0] = self._podman_path
        return argv


__all__ = ["SECRET_ENV_PREFIX", "PodmanTransport"]
