"""Local listener-owned readiness query transport; no UDP/domain side effects."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import math
import os
import socket
import stat
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from uuid import UUID, uuid4

from app.config import settings
from app.services.platform_health_observations import (
    PlatformHealthObservation,
    PlatformHealthState,
    make_platform_health_observation,
)

SYSLOG_COMPONENT_CODE = "sub.syslog_receiver"


class ListenerHealthReason(StrEnum):
    READY = "ready"
    STARTING = "starting"
    STOPPED = "stopped"
    CLOSED = "closed"
    STALE = "stale"
    UNAVAILABLE = "unavailable"
    MALFORMED = "malformed"


@dataclass(frozen=True)
class ListenerReadiness:
    reason: ListenerHealthReason
    generation: UUID
    observed_monotonic: float

    @property
    def ready(self) -> bool:
        return self.reason is ListenerHealthReason.READY


@dataclass(frozen=True)
class ListenerProbeQuery:
    socket_path: Path
    timeout_seconds: float = 2.0

    def __post_init__(self) -> None:
        if (
            isinstance(self.timeout_seconds, bool)
            or not 0 < self.timeout_seconds <= 5
            or not math.isfinite(self.timeout_seconds)
        ):
            raise ValueError("timeout must be in (0, 5]")


def default_socket_path() -> Path:
    """A private per-UID directory; override for separate listener instances."""
    if settings.syslog_health_socket is not None:
        return Path(settings.syslog_health_socket)
    return (
        Path(tempfile.gettempdir()).resolve()
        / f"dotmac-syslog-{os.getuid()}"
        / "health.sock"
    )


def validate_socket_parent(*, path: Path, create: bool = False) -> None:
    """Reject shared/writable parents and symlink traversal before local IPC."""
    if not path.is_absolute():
        raise OSError("health path must be absolute")
    if create:
        path.parent.mkdir(mode=0o700, parents=False, exist_ok=True)
    parent = path.parent.lstat()
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.getuid()
        or stat.S_IMODE(parent.st_mode) != 0o700
        or path.parent.resolve() != path.parent
    ):
        raise OSError("health parent must be private and owned by this UID")


@dataclass(frozen=True)
class HealthSocketBinding:
    socket: socket.socket
    path: Path
    identity: tuple[int, int]
    lock_fd: int

    def close(self) -> None:
        """Cleanup only our inode, retaining the lock until cleanup completes."""
        try:
            self.socket.close()
            try:
                info = self.path.lstat()
                if (info.st_dev, info.st_ino) == self.identity:
                    self.path.unlink()
            except FileNotFoundError:
                pass
        finally:
            os.close(self.lock_fd)


def bind_health_socket(*, path: Path) -> HealthSocketBinding:
    """Hold an exclusive lifetime lock; recover only a recorded stale inode.

    The private same-UID directory is the trust boundary. Unknown existing
    paths and lock contention fail closed; a live compliant peer owns its lock.
    """
    validate_socket_parent(path=path, create=True)
    lock_path = path.with_name(path.name + ".lock")
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    sock: socket.socket | None = None
    identity: tuple[int, int] | None = None
    try:
        info = os.fstat(lock_fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise OSError("invalid health lifetime lock")
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        previous = os.read(lock_fd, 128).decode("ascii")
        socket_info: os.stat_result | None
        try:
            socket_info = path.lstat()
        except FileNotFoundError:
            socket_info = None
        if socket_info is not None:
            recorded = f"{socket_info.st_dev}:{socket_info.st_ino}"
            if (
                not stat.S_ISSOCK(socket_info.st_mode)
                or socket_info.st_uid != os.getuid()
                or stat.S_IMODE(socket_info.st_mode) != 0o600
                or previous != recorded
            ):
                raise OSError("unknown health socket collision")
            # Lock acquisition proves its previous compliant owner has exited.
            path.unlink()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(path))
        info = path.lstat()
        identity = (info.st_dev, info.st_ino)
        path.chmod(0o600)
        os.lseek(lock_fd, 0, os.SEEK_SET)
        os.ftruncate(lock_fd, 0)
        os.write(lock_fd, f"{info.st_dev}:{info.st_ino}".encode("ascii"))
        os.fsync(lock_fd)
        sock.setblocking(False)
        return HealthSocketBinding(sock, path, identity, lock_fd)
    except BaseException:
        if sock is not None:
            sock.close()
        if identity is not None:
            try:
                info = path.lstat()
                if (info.st_dev, info.st_ino) == identity:
                    path.unlink()
            except FileNotFoundError:
                pass
        os.close(lock_fd)
        raise


async def query_listener_readiness(*, query: ListenerProbeQuery) -> ListenerReadiness:
    """Challenge the current listener event loop; cached prior replies cannot pass."""
    unavailable = ListenerReadiness(
        ListenerHealthReason.UNAVAILABLE, UUID(int=0), time.monotonic()
    )
    writer: asyncio.StreamWriter | None = None
    challenge = uuid4().hex
    started = time.monotonic()
    try:
        validate_socket_parent(path=query.socket_path)
        info = query.socket_path.lstat()
        if (
            not stat.S_ISSOCK(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            return unavailable

        async def exchange() -> bytes:
            nonlocal writer
            reader, writer = await asyncio.open_unix_connection(
                str(query.socket_path), limit=512
            )
            writer.write((challenge + "\n").encode("ascii"))
            await writer.drain()
            return await reader.readuntil(b"\n")

        raw = await asyncio.wait_for(exchange(), timeout=query.timeout_seconds)
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("challenge") != challenge:
            raise ValueError("invalid challenge")
        generation_value = data.get("generation")
        if not isinstance(generation_value, str) or len(generation_value) != 36:
            raise ValueError("invalid generation")
        generation = UUID(generation_value)
        observed = data["observed_monotonic"]
        if (
            generation.int == 0
            or isinstance(observed, bool)
            or not isinstance(observed, (float, int))
        ):
            raise ValueError("invalid observation")
        if not started <= observed <= time.monotonic() or not math.isfinite(observed):
            raise ValueError("invalid freshness")
        return ListenerReadiness(
            ListenerHealthReason(data["reason"]), generation, observed
        )
    except (
        OSError,
        TimeoutError,
        asyncio.IncompleteReadError,
        asyncio.LimitOverrunError,
    ):
        return unavailable
    except (ValueError, TypeError, KeyError):
        return ListenerReadiness(
            ListenerHealthReason.MALFORMED, UUID(int=0), time.monotonic()
        )
    finally:
        if writer is not None:
            writer.close()
            # Do not wait indefinitely for an untrusted/failed local peer.


_SYSLOG_PLATFORM_STATES = {
    ListenerHealthReason.READY: PlatformHealthState.HEALTHY,
    ListenerHealthReason.STARTING: PlatformHealthState.UNKNOWN,
    ListenerHealthReason.STOPPED: PlatformHealthState.UNHEALTHY,
    ListenerHealthReason.CLOSED: PlatformHealthState.UNHEALTHY,
    ListenerHealthReason.STALE: PlatformHealthState.UNHEALTHY,
    ListenerHealthReason.UNAVAILABLE: PlatformHealthState.UNKNOWN,
    ListenerHealthReason.MALFORMED: PlatformHealthState.UNKNOWN,
}


def syslog_platform_health_observation(
    *,
    readiness: ListenerReadiness,
    source_ref: str,
    received_at: datetime,
) -> PlatformHealthObservation:
    """Project receiver readiness without exposing generation or raw payloads.

    The monotonic listener reading and process generation are key-derivation
    identity only. ``received_at`` is also the wall-clock observation time for
    this bounded local challenge; Platform Health owns later freshness
    classification from that timestamp.
    """

    if (
        isinstance(readiness.observed_monotonic, bool)
        or not isinstance(readiness.observed_monotonic, int | float)
        or not 0 <= readiness.observed_monotonic <= 9_223_372_036_854_775_807
    ):
        raise ValueError("invalid observed_monotonic")
    return make_platform_health_observation(
        source_ref=source_ref,
        component_code=SYSLOG_COMPONENT_CODE,
        state=_SYSLOG_PLATFORM_STATES[readiness.reason],
        observed_at=received_at,
        received_at=received_at,
        summary=f"Syslog receiver readiness: {readiness.reason.value}",
        labels=(
            ("probe", "receiver_readiness"),
            ("reason", readiness.reason.value),
        ),
        identity=(
            f"{readiness.generation}:{readiness.observed_monotonic!r}:"
            f"{readiness.reason.value}"
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Syslog UDP receiver readiness (local, same UID)"
    )
    parser.add_argument("--socket", type=Path, default=default_socket_path())
    parser.add_argument("--timeout-seconds", type=float, default=2.0)
    parser.add_argument("--platform-health-source-ref")
    args = parser.parse_args(argv)
    try:
        query = ListenerProbeQuery(
            socket_path=args.socket, timeout_seconds=args.timeout_seconds
        )
    except ValueError:
        parser.error("--timeout-seconds must be in (0, 5]")
    outcome = asyncio.run(query_listener_readiness(query=query))
    if args.platform_health_source_ref is not None:
        try:
            observation = syslog_platform_health_observation(
                readiness=outcome,
                source_ref=args.platform_health_source_ref,
                received_at=datetime.now(UTC),
            )
        except ValueError:
            parser.error("--platform-health-source-ref is invalid")
        print(json.dumps(observation.to_document()))
    else:
        print(
            json.dumps(
                {
                    "kind": "receiver_readiness",
                    "reason": outcome.reason.value,
                    "ready": outcome.ready,
                }
            )
        )
    return 0 if outcome.ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
