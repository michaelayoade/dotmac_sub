"""Single-writer durable nonce journal and sealed, byte-preserving outbox."""

from __future__ import annotations

import fcntl
import hashlib
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def atomic(path: Path, body: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(body)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def single_instance(path: Path) -> Iterator[None]:
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


class State:
    def __init__(self, directory: Path):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if (
            directory.is_symlink()
            or directory.stat().st_uid != os.getuid()
            or directory.stat().st_mode & 0o077
        ):
            raise ValueError("unsafe_state_permissions")
        sync_directory(directory.parent)
        self.directory = directory
        for name in ("nonces", "outbox", "acknowledged", "quarantine"):
            child = directory / name
            child.mkdir(mode=0o700, exist_ok=True)
            if (
                child.is_symlink()
                or child.stat().st_uid != os.getuid()
                or child.stat().st_mode & 0o077
            ):
                raise ValueError("unsafe_state_permissions")
        sync_directory(directory)

    def nonce_path(self, nonce: str) -> Path:
        return (
            self.directory
            / "nonces"
            / (hashlib.sha256(nonce.encode()).hexdigest() + ".json")
        )

    def consume(self, nonce: str, desired: bytes) -> None:
        path = self.nonce_path(nonce)
        if path.exists():
            raise ValueError("nonce_replay")
        atomic(path, desired)

    def consumed(self, nonce: str) -> bool:
        return self.nonce_path(nonce).exists()

    def seal(self, nonce: str, envelope: bytes) -> None:
        path = self.directory / "outbox" / self.nonce_path(nonce).name
        if path.exists():
            if path.read_bytes() != envelope:
                raise ValueError("sealed_report_mutation")
            return
        atomic(path, envelope)

    def interrupted(self) -> list[bytes]:
        return [
            path.read_bytes()
            for path in sorted((self.directory / "nonces").glob("*.json"))
            if not (self.directory / "outbox" / path.name).exists()
        ]

    def unsent(self) -> list[Path]:
        return [
            path
            for path in sorted((self.directory / "outbox").glob("*.json"))
            if not (self.directory / "acknowledged" / path.name).exists()
        ]

    def acknowledge(self, path: Path) -> None:
        atomic(self.directory / "acknowledged" / path.name, b"acknowledged\n")

    def remember(self, report: bytes) -> None:
        atomic(self.directory / "last-healthy.json", report)

    def last_healthy(self) -> bytes | None:
        path = self.directory / "last-healthy.json"
        return path.read_bytes() if path.exists() else None

    def quarantine(self, raw: bytes) -> None:
        atomic(
            self.directory / "quarantine" / (hashlib.sha256(raw).hexdigest() + ".json"),
            raw,
        )
