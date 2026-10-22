"""Enrolled-host boundary for capture and disposable restore execution."""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    if len({key for key, _ in pairs}) != len(pairs):
        raise ValueError("duplicate_field")
    return dict(pairs)


class HostPurpose(StrEnum):
    CAPTURE = "capture"
    RESTORE = "restore"


class RestoreRefusal(RuntimeError):
    """Stable public refusal; never includes private process output."""


@dataclass(frozen=True)
class HostPermit:
    purpose: HostPurpose
    hostname: str
    machine_sha256: str
    docker_sha256: str

    @classmethod
    def load(cls, path: Path) -> HostPermit:
        protected(path)
        try:
            if path.stat().st_size > 4096:
                raise ValueError
            raw = json.loads(path.read_text(), object_pairs_hook=unique_object)
            if (
                not isinstance(raw, dict)
                or set(raw)
                != {"purpose", "hostname", "machine_sha256", "docker_sha256"}
                or any(type(v) is not str for v in raw.values())
            ):
                raise ValueError
            permit = cls(
                HostPurpose(raw["purpose"]),
                raw["hostname"],
                raw["machine_sha256"],
                raw["docker_sha256"],
            )
            if any(
                not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{64}", v)
                for v in (permit.machine_sha256, permit.docker_sha256)
            ):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise RestoreRefusal("host_permit_invalid") from None
        return permit

    def verify(self) -> None:
        expected = "hp-server" if self.purpose is HostPurpose.CAPTURE else "vmi3537655"
        if (
            os.geteuid() != 0
            or self.hostname != expected
            or socket.gethostname() != expected
        ):
            raise RestoreRefusal("host_identity")
        if (
            hashlib.sha256(Path("/etc/machine-id").read_bytes()).hexdigest()
            != self.machine_sha256
        ):
            raise RestoreRefusal("host_identity")
        if any(
            os.environ.get(k)
            for k in (
                "DOCKER_HOST",
                "DOCKER_CONTEXT",
                "DOCKER_TLS_VERIFY",
                "DOCKER_CERT_PATH",
            )
        ):
            raise RestoreRefusal("docker_endpoint")
        docker = Path("/usr/bin/docker")
        protected(docker)
        if hashlib.sha256(docker.read_bytes()).hexdigest() != self.docker_sha256:
            raise RestoreRefusal("docker_identity")


def protected(path: Path) -> None:
    """No mutable parent, symlink or foreign-owned execution input."""
    for entry in (path, *path.parents):
        info = entry.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise RestoreRefusal("unprotected_execution_input")
    if not path.is_file():
        raise RestoreRefusal("unprotected_execution_input")


def enrolled(purpose: HostPurpose) -> HostPermit:
    permit = HostPermit.load(Path("/etc/dotmac/gate0-host.json"))
    if permit.purpose is not purpose:
        raise RestoreRefusal("host_purpose")
    permit.verify()
    return permit
