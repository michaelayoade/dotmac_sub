"""Signed archive and local pipe boundary checks; never invoke SSH or services."""

from __future__ import annotations

import hashlib
import io
import json
import os
import select
import subprocess
import tarfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from deploy.agent import provision as p
from deploy.gate0 import proof1_runner as runner
from deploy.openbao import loopback_transport as tunnel


def signed_package(entries: tuple[tuple[str, bool, int], ...]) -> runner.RunnerPackage:
    body = io.BytesIO()
    with tarfile.open(fileobj=body, mode="w:gz") as archive:
        for name, directory, mode in entries:
            member = tarfile.TarInfo(name)
            member.mode = mode
            member.type = tarfile.DIRTYPE if directory else tarfile.REGTYPE
            content = b"#!/bin/sh\nexit 0\n"
            member.size = 0 if directory else len(content)
            archive.addfile(member, None if directory else io.BytesIO(content))
    raw = body.getvalue()
    digest, revision = hashlib.sha256(raw).hexdigest(), "a" * 40
    signer = Ed25519PrivateKey.generate()
    public = signer.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    signature = signer.sign(
        f"https://github.com/actions/runner\n{revision}\n{digest}".encode()
    )
    return runner.RunnerPackage(raw, digest, revision, signature, public)


ENTRYPOINTS = tuple((name, False, 0o755) for name in ("config.sh", "run.sh", "svc.sh"))


@pytest.mark.parametrize(
    "entries",
    [
        (*ENTRYPOINTS, ("./config.sh", False, 0o755)),
        tuple((name, True, 0o755) for name, _, _ in ENTRYPOINTS),
        tuple((name, False, 0o644) for name, _, _ in ENTRYPOINTS),
        (*ENTRYPOINTS, ("bin//tool", False, 0o755), ("bin/tool", False, 0o755)),
    ],
)
def test_signed_runner_archive_refuses_aliases_and_invalid_entrypoints(
    entries: tuple[tuple[str, bool, int], ...],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "runner"
    monkeypatch.setattr(runner, "ROOT", root)
    monkeypatch.setattr(runner, "host_guard", lambda target: None)
    with pytest.raises(p.ProvisionError, match="runner_archive"):
        runner.install(signed_package(entries), target=p.Target.SEABONE)
    assert not root.exists()


def test_signed_runner_success_is_idempotent_with_canonical_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "runner"
    original = p.protected
    monkeypatch.setattr(runner, "ROOT", root)
    monkeypatch.setattr(runner, "host_guard", lambda target: None)
    monkeypatch.setattr(p, "parents", lambda path: None)
    monkeypatch.setattr(
        p,
        "protected",
        lambda path, uid=0, directory=False: original(
            path, uid=os.getuid(), directory=directory
        ),
    )
    package = signed_package(
        tuple(("./" + name, directory, mode) for name, directory, mode in ENTRYPOINTS)
    )
    assert runner.install(package, target=p.Target.SEABONE) == package.sha256
    assert runner.install(package, target=p.Target.SEABONE) == package.sha256
    for name, _, _ in ENTRYPOINTS:
        assert (root / name).read_bytes() == b"#!/bin/sh\nexit 0\n"
        assert (root / name).stat().st_mode & 0o777 == 0o755


@dataclass
class PipeProcess:
    stdout: BinaryIO
    termination_timeout: bool = False
    kill_timeout: bool = False
    terminated: bool = False
    killed: bool = False
    waits: int = 0

    def poll(self) -> None:
        return None

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: int) -> int:
        self.waits += 1
        assert timeout == 5
        if self.termination_timeout and self.waits == 1 or self.kill_timeout:
            raise subprocess.TimeoutExpired("mock-ssh", timeout)
        return 0


@dataclass
class FakeHealth:
    cluster_id: str
    calls: int = 0

    def open(self, url: str, *, timeout: int) -> io.BytesIO:
        self.calls += 1
        assert url == "http://127.0.0.1:18200/v1/sys/health" and timeout == 2
        return io.BytesIO(
            json.dumps(
                {"cluster_id": self.cluster_id, "sealed": False, "initialized": True}
            ).encode()
        )


@dataclass
class TransportBoundary:
    process: PipeProcess
    writer: int
    chunks: list[bytes | None]
    timeouts: list[float]
    health: FakeHealth
    plan: tunnel.LoopbackPlan


@pytest.fixture
def transport_boundary(monkeypatch: pytest.MonkeyPatch) -> Iterator[TransportBoundary]:
    reader, writer = os.pipe()
    stream = os.fdopen(reader, "rb", buffering=0)
    process = PipeProcess(stream)
    boundary = TransportBoundary(
        process,
        writer,
        [b"DOTMAC_TUNNEL_READY\n"],
        [],
        FakeHealth("a" * 36),
        tunnel.LoopbackPlan(
            "observe.example", "proof", Path("/pin"), "a" * 64, "a" * 36
        ),
    )
    real_select = select.select

    def ready(
        readers: list[int], writers: list[int], errors: list[int], timeout: float
    ) -> tuple[list[int], list[int], list[int]]:
        boundary.timeouts.append(timeout)
        chunk = boundary.chunks.pop(0) if boundary.chunks else None
        if chunk is None:
            return [], [], []
        os.write(writer, chunk)
        return real_select(readers, writers, errors, 0)

    def spawn(argv: tuple[str, ...], **kwargs: object) -> PipeProcess:
        assert argv[0] == "/usr/bin/ssh"
        assert "StrictHostKeyChecking=yes" in argv
        assert "127.0.0.1:18200:127.0.0.1:8200" in argv
        assert kwargs["stderr"] is subprocess.DEVNULL
        return process

    monkeypatch.setattr(tunnel.LoopbackPlan, "validate", lambda plan: None)
    monkeypatch.setattr(tunnel.subprocess, "Popen", spawn)
    monkeypatch.setattr(tunnel.select, "select", ready)
    monkeypatch.setattr(
        tunnel.urllib.request, "build_opener", lambda *handlers: boundary.health
    )
    try:
        yield boundary
    finally:
        stream.close()
        os.close(writer)


def test_transport_fragmented_ready_success_and_cleanup(
    transport_boundary: TransportBoundary,
) -> None:
    boundary = transport_boundary
    boundary.chunks[:] = [b"DOT", b"MAC_TUNNEL_", b"READY\n"]
    with tunnel.open_transport(boundary.plan) as endpoint:
        assert endpoint == "http://127.0.0.1:18200"
        assert not boundary.process.terminated
    assert boundary.process.terminated and boundary.process.waits == 1
    assert boundary.health.calls == 1
    assert boundary.process.stdout.closed


@pytest.mark.parametrize("chunks", [[None], [b"D", None], [b"X" * 64], [b"bad\n"]])
def test_transport_readiness_failure_is_bounded_and_cleans_up(
    transport_boundary: TransportBoundary, chunks: list[bytes | None]
) -> None:
    boundary = transport_boundary
    boundary.chunks[:] = chunks
    with pytest.raises(RuntimeError, match="transport_forward_unproven"):
        with tunnel.open_transport(boundary.plan):
            pytest.fail("unverified transport yielded")
    assert boundary.process.terminated and boundary.health.calls == 0
    assert boundary.process.stdout.closed
    assert boundary.timeouts and all(0 < timeout <= 15 for timeout in boundary.timeouts)


def test_transport_authority_mismatch_cleans_up(
    transport_boundary: TransportBoundary,
) -> None:
    boundary = transport_boundary
    boundary.health.cluster_id = "b" * 36
    with pytest.raises(RuntimeError, match="transport_authority_mismatch"):
        with tunnel.open_transport(boundary.plan):
            pytest.fail("wrong authority yielded")
    assert boundary.process.terminated and boundary.process.waits == 1

    assert boundary.process.stdout.closed


@pytest.mark.parametrize("kill_timeout", [False, True])
def test_transport_failed_termination_uses_bounded_kill(
    transport_boundary: TransportBoundary, kill_timeout: bool
) -> None:
    boundary = transport_boundary
    boundary.process.termination_timeout, boundary.process.kill_timeout = (
        True,
        kill_timeout,
    )

    def run() -> None:
        with tunnel.open_transport(boundary.plan):
            pass

    if kill_timeout:
        with pytest.raises(subprocess.TimeoutExpired):
            run()
    else:
        run()
    assert boundary.process.terminated and boundary.process.killed
    assert boundary.process.waits == 2
    assert boundary.process.stdout.closed


def test_transport_partial_ready_obeys_absolute_deadline(
    transport_boundary: TransportBoundary, monkeypatch: pytest.MonkeyPatch
) -> None:
    boundary = transport_boundary
    boundary.chunks[:] = [b"D"]
    clock = iter((0.0, 1.0, 16.0))
    monkeypatch.setattr(tunnel.time, "monotonic", lambda: next(clock))
    with pytest.raises(RuntimeError, match="transport_forward_unproven"):
        with tunnel.open_transport(boundary.plan):
            pytest.fail("expired readiness yielded")
    assert boundary.timeouts == [14.0]
    assert boundary.process.terminated and boundary.process.stdout.closed
    assert boundary.health.calls == 0
