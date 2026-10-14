"""Component health unit tests; execute only on GitHub-hosted CI."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import time
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypeVar
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from app.services import poller_health
from app.services.poller_health import (
    PollerHealthReason,
    PollerLivenessQuery,
    PollerSnapshot,
    decode_poller_health,
)
from app.syslog.health import (
    ListenerHealthReason,
    ListenerProbeQuery,
    bind_health_socket,
    query_listener_readiness,
)
from app.syslog.listener import SyslogListener, SyslogProtocol

T = TypeVar("T")
NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)


def _run(coro: Coroutine[object, object, T]) -> T:
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coro).result()


def _snapshot(*, observed_at: datetime = NOW, failing: int = 0) -> PollerSnapshot:
    return PollerSnapshot(observed_at, 0.125, 12, 2, 2 - failing, failing)


def _wire() -> dict[str, object]:
    return {
        "ts": NOW.isoformat(),
        "cycle_seconds": 0.125,
        "poll_count": 12,
        "devices_total": 2,
        "devices_ok": 2,
        "devices_failing": 0,
        "device_failures": [],
    }


@pytest.mark.parametrize(
    "age,reason",
    [
        (0, PollerHealthReason.LIVE),
        (60, PollerHealthReason.LIVE),
        (61, PollerHealthReason.STALE),
        (-1, PollerHealthReason.FUTURE),
    ],
)
def test_poller_freshness(
    monkeypatch: pytest.MonkeyPatch, age: float, reason: PollerHealthReason
) -> None:
    monkeypatch.setattr(
        poller_health,
        "load_poller_health",
        lambda: _snapshot(observed_at=NOW - timedelta(seconds=age)),
    )
    outcome = poller_health.query_poller_liveness(query=PollerLivenessQuery(60, NOW))
    assert outcome.reason is reason


def test_fresh_failed_cycle_and_idle_only_prove_liveness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # This is exactly the incumbent post-caught-failure wire contract: no success marker.
    for snapshot in (_snapshot(failing=2), PollerSnapshot(NOW, 0, 0, 0, 0, 0)):

        def load_snapshot(
            current_snapshot: PollerSnapshot = snapshot,
        ) -> PollerSnapshot:
            return current_snapshot

        monkeypatch.setattr(poller_health, "load_poller_health", load_snapshot)
        outcome = poller_health.query_poller_liveness(
            query=PollerLivenessQuery(60, NOW)
        )
        assert outcome.live
        assert not hasattr(outcome, "ready")
        assert outcome.devices_failing == snapshot.devices_failing


@pytest.mark.parametrize(
    "key,value",
    [
        ("ts", None),
        ("ts", "2026-10-14T12:00:00"),
        ("cycle_seconds", True),
        ("cycle_seconds", float("nan")),
        ("cycle_seconds", 10**1000),
        ("devices_total", -1),
        ("devices_ok", 3),
        ("poll_count", True),
        ("device_failures", "invalid"),
    ],
)
def test_poller_rejects_malformed_projection(key: str, value: object) -> None:
    wire = _wire()
    wire[key] = value
    assert decode_poller_health(raw=json.dumps(wire)) is None


@pytest.mark.parametrize("raw", [b"", b"[]", b"null", b"{", b"x" * 262145])
def test_poller_rejects_bad_wire(raw: bytes) -> None:
    assert decode_poller_health(raw=raw) is None


def test_poller_redis_failure_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    client.getrange.side_effect = OSError("private exception detail")
    monkeypatch.setattr(poller_health, "_get_redis", lambda: client)
    assert (
        poller_health.query_poller_liveness(query=PollerLivenessQuery(60, NOW)).reason
        is PollerHealthReason.UNAVAILABLE
    )
    client.getrange.assert_called_once_with(poller_health.POLLER_HEALTH_KEY, 0, 262144)


def test_poller_cli_exit_and_safe_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(poller_health, "load_poller_health", lambda: None)
    assert poller_health.main(["--max-age-seconds", "60"]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "kind": "liveness",
        "reason": "unavailable",
        "live": False,
        "devices_failing": None,
    }
    with pytest.raises(SystemExit) as error:
        poller_health.main(["--max-age-seconds", "nan"])
    assert error.value.code == 2


def test_typed_poller_snapshot_preserves_metric_and_noc_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.metrics import _PollerHealthCollector
    from app.services import operational_checks

    wire = _wire()
    device_id = uuid4()
    wire["devices_ok"] = 1
    wire["devices_failing"] = 1
    wire["device_failures"] = [
        {
            "device_id": str(device_id),
            "name": "NAS",
            "host": "192.0.2.1",
            "error_category": "timeout",
            "consecutive_failures": 2,
            "last_attempt_at": NOW.isoformat(),
            "last_success_at": None,
            "next_attempt_at": (NOW + timedelta(seconds=30)).isoformat(),
            "services_without_live_bandwidth": 4,
        }
    ]
    snapshot = decode_poller_health(raw=json.dumps(wire))
    assert snapshot is not None
    monkeypatch.setattr(poller_health, "load_poller_health", lambda: snapshot)
    metrics = {
        metric.name: metric.samples[0].value
        for metric in _PollerHealthCollector().collect()
    }
    assert metrics["bandwidth_poller_devices_failing"] == 1
    assert metrics["bandwidth_poller_devices_ok"] == 1
    assert metrics["bandwidth_poller_devices_total"] == 2
    assert metrics["bandwidth_poller_cycle_seconds"] == 0.125
    (failure,) = operational_checks.bandwidth_device_failures(snapshot)
    assert failure.device_id == device_id
    assert failure.services_without_live_bandwidth == 4
    check = operational_checks._bandwidth_check(snapshot, now=NOW).to_dict()
    assert check["key"] == "bandwidth_poller"
    assert check["needs_attention"] is True
    assert "1 of 2 routers" in str(check["last_result"])


def _listener(path: Path) -> SyslogListener:
    listener = SyslogListener(health_socket_path=path)
    listener._running = True
    listener._transport = MagicMock(spec=asyncio.DatagramTransport)
    listener._transport.is_closing.return_value = False
    return listener


def test_listener_startup_closed_stale_stop_and_generation(tmp_path: Path) -> None:
    listener = _listener(tmp_path / "health.sock")
    assert listener.receiver_readiness().reason is ListenerHealthReason.STARTING
    listener._last_loop_progress = time.monotonic()
    generation = listener.receiver_readiness().generation
    assert listener.receiver_readiness().ready
    listener._last_loop_progress = time.monotonic() - 4
    assert listener.receiver_readiness().reason is ListenerHealthReason.STALE
    listener._last_loop_progress = time.monotonic() + 4
    assert listener.receiver_readiness().reason is ListenerHealthReason.STALE
    listener._last_loop_progress = time.monotonic()
    listener._transport.is_closing.return_value = True
    assert listener.receiver_readiness().reason is ListenerHealthReason.CLOSED
    _run(listener.stop())
    assert listener.receiver_readiness().reason is ListenerHealthReason.STOPPED
    assert SyslogListener().receiver_readiness().generation != generation


def test_listener_protocol_failure_revokes_readiness(tmp_path: Path) -> None:
    listener = _listener(tmp_path / "health.sock")
    listener._last_loop_progress = time.monotonic()
    protocol = SyslogProtocol(AsyncMock(), listener._receiver_failed)
    protocol.connection_lost(None)
    assert not listener.receiver_readiness().ready


def test_healthy_idle_real_challenge_and_cleanup(tmp_path: Path) -> None:
    path = tmp_path / "private" / "health.sock"
    listener = _listener(path)

    async def drive() -> None:
        listener._health_binding = bind_health_socket(path=path)
        listener._health_server = await asyncio.start_unix_server(
            listener._answer_health, sock=listener._health_binding.socket, limit=128
        )
        listener._last_loop_progress = time.monotonic()
        result = await query_listener_readiness(query=ListenerProbeQuery(path))
        assert result.ready
        assert result.generation == listener._generation
        assert listener._event_count == 0
        assert listener._ont_event_count == 0
        await listener.stop()
        assert not path.exists()
        assert not (
            await query_listener_readiness(query=ListenerProbeQuery(path))
        ).ready

    _run(drive())


def test_bind_failure_never_opens_health_endpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    listener = SyslogListener(health_socket_path=tmp_path / "private" / "health.sock")

    async def drive() -> None:
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(
            loop,
            "create_datagram_endpoint",
            AsyncMock(side_effect=OSError("bind failed")),
        )
        monkeypatch.setattr("app.syslog.listener.SYSLOG_ENABLED", True)
        await listener.run()
        assert not listener.receiver_readiness().ready
        assert listener._health_binding is None
        assert not listener._health_socket_path.exists()

    _run(drive())


def test_lifetime_lock_preserves_live_peer_and_recovers_crash(tmp_path: Path) -> None:
    path = tmp_path / "private" / "health.sock"
    first = bind_health_socket(path=path)
    identity = path.stat().st_ino
    with pytest.raises(OSError):
        bind_health_socket(path=path)
    assert path.stat().st_ino == identity
    # Emulate abrupt process exit: kernel releases socket+lock, pathname remains.
    first.socket.close()
    os.close(first.lock_fd)
    second = bind_health_socket(path=path)
    second.close()
    assert not path.exists()


def test_unknown_socket_and_symlink_are_never_replaced(tmp_path: Path) -> None:
    path = tmp_path / "private" / "health.sock"
    path.parent.mkdir(mode=0o700)
    unknown = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        unknown.bind(str(path))
        path.chmod(0o600)
        with pytest.raises(OSError):
            bind_health_socket(path=path)
        assert path.exists()
    finally:
        unknown.close()
        path.unlink()
    target = tmp_path / "untouched"
    target.write_text("evidence")
    path.symlink_to(target)
    with pytest.raises(OSError):
        bind_health_socket(path=path)
    assert target.read_text() == "evidence"


def test_shared_parent_is_rejected(tmp_path: Path) -> None:
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o755)
    with pytest.raises(OSError):
        bind_health_socket(path=parent / "health.sock")


@pytest.mark.parametrize(
    "bad_field,bad_value",
    [
        ("generation", 12),
        ("generation", []),
        ("generation", {}),
        ("generation", str(UUID(int=0))),
        ("challenge", "saved-previous-response"),
        ("observed_monotonic", 10**1000),
        ("observed_monotonic", -1),
        ("observed_monotonic", float("nan")),
        ("reason", "invented"),
    ],
)
def test_probe_rejects_bad_or_prior_response(
    tmp_path: Path, bad_field: str, bad_value: object
) -> None:
    path = tmp_path / "private" / "health.sock"

    async def drive() -> None:
        binding = bind_health_socket(path=path)

        async def answer(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            challenge = (await reader.readline()).decode().strip()
            reply: dict[str, object] = {
                "generation": str(uuid4()),
                "challenge": challenge,
                "observed_monotonic": time.monotonic(),
                "reason": "ready",
            }
            reply[bad_field] = bad_value
            writer.write((json.dumps(reply) + "\n").encode())
            await writer.drain()
            writer.close()

        server = await asyncio.start_unix_server(answer, sock=binding.socket)
        try:
            result = await query_listener_readiness(query=ListenerProbeQuery(path))
            assert not result.ready
        finally:
            server.close()
            await server.wait_closed()
            binding.close()

    _run(drive())


def test_stalled_peer_times_out_and_missing_socket_fails(tmp_path: Path) -> None:
    path = tmp_path / "private" / "health.sock"

    async def drive() -> None:
        binding = bind_health_socket(path=path)
        held: set[asyncio.StreamWriter] = set()

        async def stalled(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            held.add(writer)
            await reader.read()
            held.discard(writer)
            writer.close()

        server = await asyncio.start_unix_server(stalled, sock=binding.socket)
        try:
            start = time.monotonic()
            result = await query_listener_readiness(
                query=ListenerProbeQuery(path, 0.05)
            )
            assert not result.ready
            assert time.monotonic() - start < 1
        finally:
            for writer in tuple(held):
                writer.close()
            server.close()
            await server.wait_closed()
            binding.close()

    _run(drive())


def test_real_poller_module_cli_is_unavailable_without_redis() -> None:
    import subprocess
    import sys

    env = os.environ.copy()
    env.pop("REDIS_URL", None)
    result = subprocess.run(
        [sys.executable, "-m", "app.services.poller_health", "--max-age-seconds", "60"],
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout)["reason"] == "unavailable"
    assert "Traceback" not in result.stderr


def test_real_syslog_module_cli_queries_idle_receiver(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import sys

    path = tmp_path / "private" / "health.sock"
    listener = SyslogListener(health_socket_path=path)
    transport = MagicMock(spec=asyncio.DatagramTransport)
    transport.is_closing.return_value = False

    async def drive() -> None:
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(
            loop,
            "create_datagram_endpoint",
            AsyncMock(return_value=(transport, SyslogProtocol(AsyncMock()))),
        )
        monkeypatch.setattr("app.syslog.listener.SYSLOG_ENABLED", True)
        task = asyncio.create_task(listener.run())
        child: asyncio.subprocess.Process | None = None
        try:

            async def wait_ready() -> None:
                while not listener.receiver_readiness().ready:
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(wait_ready(), timeout=2)
            child = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "app.syslog.health",
                "--socket",
                str(path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(child.communicate(), timeout=5)
            assert child.returncode == 0
            assert json.loads(stdout) == {
                "kind": "receiver_readiness",
                "reason": "ready",
                "ready": True,
            }
            assert b"Traceback" not in stderr
            assert listener._event_count == listener._ont_event_count == 0
        finally:
            if child is not None and child.returncode is None:
                child.kill()
                await child.wait()
            await listener.stop()
            await asyncio.wait_for(task, timeout=2)

    _run(drive())


def test_real_listener_loop_progresses_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "private" / "health.sock"
    listener = SyslogListener(health_socket_path=path)
    transport = MagicMock(spec=asyncio.DatagramTransport)
    transport.is_closing.return_value = False

    async def drive() -> None:
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(
            loop,
            "create_datagram_endpoint",
            AsyncMock(return_value=(transport, SyslogProtocol(AsyncMock()))),
        )
        monkeypatch.setattr("app.syslog.listener.SYSLOG_ENABLED", True)
        task = asyncio.create_task(listener.run())
        try:

            async def wait_ready() -> None:
                while not listener.receiver_readiness().ready:
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(wait_ready(), timeout=2)
            assert listener._event_count == 0
            assert (
                await query_listener_readiness(query=ListenerProbeQuery(path))
            ).ready
            await listener.stop()
            await asyncio.wait_for(task, timeout=2)
            transport.close.assert_called_once()
            assert not path.exists()
        finally:
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

    _run(drive())


def test_partial_socket_startup_failure_removes_own_inode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "private" / "health.sock"

    def fail_chmod(self: Path, mode: int) -> None:
        raise OSError("chmod failed")

    monkeypatch.setattr(Path, "chmod", fail_chmod)
    with pytest.raises(OSError):
        bind_health_socket(path=path)
    assert not path.exists()


@pytest.mark.parametrize("budget", [True, 10**1000, float("inf"), float("nan"), 0, -1])
def test_typed_probe_budgets_reject_invalid_values(
    tmp_path: Path, budget: float
) -> None:
    with pytest.raises(ValueError):
        ListenerProbeQuery(tmp_path / "health.sock", budget)
    with pytest.raises(ValueError):
        PollerLivenessQuery(budget, NOW)
