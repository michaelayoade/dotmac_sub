"""Component health unit tests; execute only on GitHub-hosted CI."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import time
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import TypeVar
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from app.config import settings
from app.services import platform_health_observations, poller_health
from app.services.platform_health_observations import (
    PlatformHealthObservation,
    PlatformHealthState,
    make_platform_health_observation,
)
from app.services.poller_health import (
    PollerDeviceFailure,
    PollerFailureCategory,
    PollerHealthReason,
    PollerLivenessQuery,
    PollerObservationIdentity,
    PollerSnapshot,
    decode_poller_health,
    poller_observation_identity,
    poller_platform_health_observation,
)
from app.syslog import health as syslog_health
from app.syslog.health import (
    ListenerHealthReason,
    ListenerObservationIdentity,
    ListenerProbeQuery,
    ListenerReadiness,
    bind_health_socket,
    query_listener_readiness,
    syslog_platform_health_observation,
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


def test_syslog_socket_consumes_bootstrap_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "private" / "health.sock"
    monkeypatch.setattr(
        syslog_health, "settings", replace(settings, syslog_health_socket=str(path))
    )
    # A later raw environment edit cannot override the bootstrap owner's value.
    monkeypatch.setenv("SYSLOG_HEALTH_SOCKET", str(tmp_path / "other.sock"))
    assert syslog_health.default_socket_path() == path
    assert SyslogListener()._health_socket_path == path
    binding = bind_health_socket(path=syslog_health.default_socket_path())
    try:
        assert path.is_socket()
        assert path.parent.stat().st_mode & 0o777 == 0o700
        assert path.stat().st_mode & 0o777 == 0o600
    finally:
        binding.close()


def test_syslog_unset_bootstrap_override_keeps_private_uid_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        syslog_health, "settings", replace(settings, syslog_health_socket=None)
    )
    monkeypatch.setattr(syslog_health.tempfile, "gettempdir", lambda: str(tmp_path))
    assert syslog_health.default_socket_path() == (
        tmp_path.resolve() / f"dotmac-syslog-{os.getuid()}" / "health.sock"
    )


@pytest.mark.parametrize("override", ["", "relative/health.sock"])
def test_syslog_bootstrap_override_never_falls_back_on_invalid_path(
    monkeypatch: pytest.MonkeyPatch, override: str
) -> None:
    monkeypatch.setattr(
        syslog_health, "settings", replace(settings, syslog_health_socket=override)
    )
    path = syslog_health.default_socket_path()
    with pytest.raises(OSError, match="health path must be absolute"):
        bind_health_socket(path=path)
    result = _run(query_listener_readiness(query=ListenerProbeQuery(path)))
    assert result.reason is ListenerHealthReason.UNAVAILABLE


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
        assert result.observation_identity == ListenerObservationIdentity(
            listener._generation, result.observed_monotonic
        )
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
            assert result.observation_identity is None
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


@pytest.mark.parametrize("use_environment_override", [False, True])
def test_real_syslog_module_cli_queries_idle_receiver(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, use_environment_override: bool
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
            env = os.environ.copy()
            env["SYSLOG_HEALTH_SOCKET"] = str(
                path if use_environment_override else tmp_path / "wrong.sock"
            )
            socket_args = [] if use_environment_override else ["--socket", str(path)]
            child = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "app.syslog.health",
                *socket_args,
                env=env,
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


def _platform_observation(**overrides: object) -> PlatformHealthObservation:
    values: dict[str, object] = {
        "source_ref": "deployment:sub:test",
        "component_code": "sub.example",
        "state": PlatformHealthState.HEALTHY,
        "observed_at": NOW,
        "received_at": NOW + timedelta(seconds=1),
        "summary": "Example component observed",
        "labels": (("reason", "ready"),),
        "identity": "example:1",
    }
    values.update(overrides)
    return make_platform_health_observation(**values)  # type: ignore[arg-type]


def test_platform_health_observation_is_bounded_and_deterministic() -> None:
    first = _platform_observation(labels=(("b", "2"), ("a", "1")))
    second = _platform_observation(labels=(("a", "1"), ("b", "2")))
    changed_identity = _platform_observation(identity="example:2")

    assert first == second
    assert first.observation_key != changed_identity.observation_key
    assert len(first.observation_key) == 64
    assert first.to_document() == {
        "source_ref": "deployment:sub:test",
        "observation_key": first.observation_key,
        "component_code": "sub.example",
        "state": "healthy",
        "observed_at": NOW.isoformat(),
        "received_at": (NOW + timedelta(seconds=1)).isoformat(),
        "summary": "Example component observed",
        "labels": {"a": "1", "b": "2"},
    }
    assert platform_health_observations.PlatformHealthState is PlatformHealthState


@pytest.mark.parametrize(
    "overrides",
    [
        {"source_ref": "contains space"},
        {"source_ref": "x" * 201},
        {"component_code": ""},
        {"observed_at": NOW.replace(tzinfo=None)},
        {"received_at": NOW - timedelta(seconds=1)},
        {"summary": "bad\nsummary"},
        {"summary": "x" * 241},
        {"labels": (("reason", "ready"), ("reason", "duplicate"))},
        {"labels": tuple((f"k{index}", "v") for index in range(21))},
        {"labels": (("k" * 81, "v"),)},
        {"labels": (("reason", "v" * 161),)},
        {"identity": "bad\nidentity"},
    ],
)
def test_platform_health_observation_rejects_unbounded_input(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        _platform_observation(**overrides)


@pytest.mark.parametrize(
    "snapshot,state",
    [
        (_snapshot(), PlatformHealthState.HEALTHY),
        (_snapshot(failing=1), PlatformHealthState.DEGRADED),
        (_snapshot(failing=2), PlatformHealthState.UNHEALTHY),
        (PollerSnapshot(NOW, 0, 0, 0, 0, 0), PlatformHealthState.HEALTHY),
    ],
)
def test_poller_platform_health_state_uses_only_aggregate_counts(
    snapshot: PollerSnapshot, state: PlatformHealthState
) -> None:
    canary_snapshot = replace(
        snapshot,
        device_failures=(
            PollerDeviceFailure(
                device_id=uuid4(),
                name="CANARY-NAME",
                host="CANARY-HOST",
                error_category=PollerFailureCategory.TIMEOUT,
                consecutive_failures=2,
                last_attempt_at=NOW,
                last_success_at=None,
                next_attempt_at=None,
                services_without_live_bandwidth=3,
            ),
        )
        if snapshot.devices_failing
        else (),
    )
    observation = poller_platform_health_observation(
        snapshot=canary_snapshot,
        source_ref="deployment:sub:test",
        received_at=NOW + timedelta(seconds=2),
    )

    assert observation.component_code == "sub.bandwidth_poller"
    assert observation.state is state
    assert observation.observed_at == NOW
    assert observation.labels == (
        ("devices_failing", str(snapshot.devices_failing)),
        ("devices_total", str(snapshot.devices_total)),
        ("probe", "loop_liveness"),
        ("reason", "snapshot_available"),
    )
    serialized = json.dumps(observation.to_document())
    assert "CANARY" not in serialized
    if canary_snapshot.device_failures:
        assert str(canary_snapshot.device_failures[0].device_id) not in serialized


@pytest.mark.parametrize(
    "snapshot,reason",
    [
        (None, "unavailable"),
        (_snapshot(observed_at=NOW + timedelta(seconds=5)), "future_timestamp"),
    ],
)
def test_poller_platform_health_missing_or_future_snapshot_is_unknown(
    snapshot: PollerSnapshot | None, reason: str
) -> None:
    observation = poller_platform_health_observation(
        snapshot=snapshot,
        source_ref="deployment:sub:test",
        received_at=NOW,
    )

    assert observation.state is PlatformHealthState.UNKNOWN
    assert observation.observed_at == observation.received_at == NOW
    assert dict(observation.labels)["reason"] == reason


@pytest.mark.parametrize(
    "reason,state",
    [
        (ListenerHealthReason.READY, PlatformHealthState.HEALTHY),
        (ListenerHealthReason.STARTING, PlatformHealthState.UNKNOWN),
        (ListenerHealthReason.STOPPED, PlatformHealthState.UNHEALTHY),
        (ListenerHealthReason.CLOSED, PlatformHealthState.UNHEALTHY),
        (ListenerHealthReason.STALE, PlatformHealthState.UNHEALTHY),
        (ListenerHealthReason.UNAVAILABLE, PlatformHealthState.UNKNOWN),
        (ListenerHealthReason.MALFORMED, PlatformHealthState.UNKNOWN),
    ],
)
def test_syslog_platform_health_state_mapping(
    reason: ListenerHealthReason, state: PlatformHealthState
) -> None:
    readiness = ListenerReadiness(reason, uuid4(), time.monotonic())
    observation = syslog_platform_health_observation(
        readiness=readiness,
        source_ref="deployment:sub:test",
        received_at=NOW,
    )

    assert observation.component_code == "sub.syslog_receiver"
    assert observation.state is state
    assert dict(observation.labels) == {
        "probe": "receiver_readiness",
        "reason": reason.value,
    }
    assert str(readiness.generation) not in json.dumps(observation.to_document())


def test_syslog_platform_health_rejects_invalid_monotonic() -> None:
    with pytest.raises(ValueError):
        syslog_platform_health_observation(
            readiness=ListenerReadiness(
                ListenerHealthReason.READY, uuid4(), float("nan")
            ),
            source_ref="deployment:sub:test",
            received_at=NOW,
        )
    with pytest.raises(ValueError):
        syslog_platform_health_observation(
            readiness=ListenerReadiness(ListenerHealthReason.READY, uuid4(), 10**1000),
            source_ref="deployment:sub:test",
            received_at=NOW,
        )


def test_poller_platform_health_rejects_naive_snapshot_time() -> None:
    with pytest.raises(ValueError):
        poller_platform_health_observation(
            snapshot=_snapshot(observed_at=NOW.replace(tzinfo=None)),
            source_ref="deployment:sub:test",
            received_at=NOW,
        )


def test_poller_cli_can_emit_platform_health_observation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    snapshot = _snapshot(observed_at=datetime.now(UTC), failing=1)
    monkeypatch.setattr(poller_health, "load_poller_health", lambda: snapshot)

    assert (
        poller_health.main(
            [
                "--max-age-seconds",
                "60",
                "--platform-health-source-ref",
                "deployment:sub:test",
            ]
        )
        == 0
    )
    document = json.loads(capsys.readouterr().out)
    assert document["component_code"] == "sub.bandwidth_poller"
    assert document["state"] == "degraded"
    assert document["source_ref"] == "deployment:sub:test"
    assert set(document) == {
        "source_ref",
        "observation_key",
        "component_code",
        "state",
        "observed_at",
        "received_at",
        "summary",
        "labels",
    }

    stale = _snapshot(observed_at=datetime.now(UTC) - timedelta(seconds=120))
    monkeypatch.setattr(poller_health, "load_poller_health", lambda: stale)
    assert (
        poller_health.main(
            [
                "--max-age-seconds",
                "60",
                "--platform-health-source-ref",
                "deployment:sub:test",
            ]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out)["state"] == "healthy"

    with pytest.raises(SystemExit) as error:
        poller_health.main(
            [
                "--max-age-seconds",
                "60",
                "--platform-health-source-ref",
                "contains space",
            ]
        )
    assert error.value.code == 2


def test_poller_cli_stamps_receipt_after_loading_snapshot(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    received_at = NOW + timedelta(seconds=2)
    snapshot = _snapshot(observed_at=NOW + timedelta(seconds=1))
    loaded = False

    class ProbeClock:
        @staticmethod
        def now(tz: object) -> datetime:
            assert tz is UTC
            return received_at if loaded else NOW

    def load_snapshot() -> PollerSnapshot:
        nonlocal loaded
        loaded = True
        return snapshot

    monkeypatch.setattr(poller_health, "datetime", ProbeClock)
    monkeypatch.setattr(poller_health, "load_poller_health", load_snapshot)

    # Local liveness keeps its incumbent query-start timestamp and exit policy.
    assert (
        poller_health.main(
            [
                "--max-age-seconds",
                "60",
                "--platform-health-source-ref",
                "deployment:sub:test",
            ]
        )
        == 1
    )
    document = json.loads(capsys.readouterr().out)
    assert document["state"] == "healthy"
    assert document["labels"]["reason"] == "snapshot_available"
    assert document["observed_at"] == snapshot.observed_at.isoformat()
    assert document["received_at"] == received_at.isoformat()


def test_syslog_cli_can_emit_platform_health_observation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    readiness = ListenerReadiness(ListenerHealthReason.READY, uuid4(), time.monotonic())

    async def fake_query(*, query: ListenerProbeQuery) -> ListenerReadiness:
        return readiness

    monkeypatch.setattr(syslog_health, "query_listener_readiness", fake_query)
    assert (
        syslog_health.main(
            [
                "--socket",
                str(tmp_path / "health.sock"),
                "--platform-health-source-ref",
                "deployment:sub:test",
            ]
        )
        == 0
    )
    document = json.loads(capsys.readouterr().out)
    assert document["component_code"] == "sub.syslog_receiver"
    assert document["state"] == "healthy"
    assert document["labels"] == {
        "probe": "receiver_readiness",
        "reason": "ready",
    }

    with pytest.raises(SystemExit) as error:
        syslog_health.main(
            [
                "--socket",
                str(tmp_path / "health.sock"),
                "--platform-health-source-ref",
                "contains space",
            ]
        )
    assert error.value.code == 2


def test_poller_native_identity_retains_exact_source_coordinates() -> None:
    observed = NOW.astimezone(tz=timezone(timedelta(hours=1)))
    snapshot = replace(_snapshot(), observed_at=observed)
    identity = snapshot.observation_identity
    assert identity == PollerObservationIdentity(snapshot.poll_count, observed)
    assert identity.observed_at is observed
    assert poller_observation_identity(snapshot=snapshot) == identity
    assert (
        replace(snapshot, devices_failing=2, devices_ok=0).observation_identity
        == identity
    )
    assert replace(snapshot, cycle_seconds=99).observation_identity == identity
    assert (
        replace(snapshot, poll_count=snapshot.poll_count + 1).observation_identity
        != identity
    )
    assert (
        replace(
            snapshot, observed_at=observed + timedelta(seconds=1)
        ).observation_identity
        != identity
    )
    with pytest.raises(FrozenInstanceError):
        identity.poll_count = 99  # type: ignore[misc]


@pytest.mark.parametrize("raw", [b"", b"{}", b"not-json"])
def test_missing_or_malformed_poller_source_has_no_native_identity(raw: bytes) -> None:
    assert poller_observation_identity(snapshot=None) is None
    assert poller_observation_identity(snapshot=decode_poller_health(raw=raw)) is None


def test_unavailable_poller_reader_has_no_native_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(poller_health, "_get_redis", lambda: None)
    assert (
        poller_observation_identity(snapshot=poller_health.load_poller_health()) is None
    )


@pytest.mark.parametrize("age", [-1, 0, 61])
def test_native_poller_identity_does_not_classify_freshness(age: int) -> None:
    snapshot = _snapshot(observed_at=NOW - timedelta(seconds=age))
    assert snapshot.observation_identity == PollerObservationIdentity(
        12, snapshot.observed_at
    )


def test_listener_native_identity_is_independent_of_health_reason() -> None:
    generation = uuid4()
    observed = 123.5
    readiness = ListenerReadiness(ListenerHealthReason.READY, generation, observed)
    identity = readiness.observation_identity
    assert identity == ListenerObservationIdentity(generation, observed)
    assert identity is not None
    for reason in ListenerHealthReason:
        assert replace(readiness, reason=reason).observation_identity == identity
    assert replace(readiness, generation=uuid4()).observation_identity != identity
    assert (
        replace(readiness, observed_monotonic=observed + 1).observation_identity
        != identity
    )
    with pytest.raises(FrozenInstanceError):
        identity.observed_monotonic = 999  # type: ignore[misc]


@pytest.mark.parametrize(
    "reason", [ListenerHealthReason.UNAVAILABLE, ListenerHealthReason.MALFORMED]
)
def test_synthetic_listener_failure_has_no_native_identity(
    reason: ListenerHealthReason,
) -> None:
    readiness = ListenerReadiness(reason, UUID(int=0), 123.5)
    assert readiness.observation_identity is None
    assert replace(readiness, observed_monotonic=999).observation_identity is None


def test_missing_listener_endpoint_has_no_native_identity(tmp_path: Path) -> None:
    readiness = _run(
        query_listener_readiness(
            query=ListenerProbeQuery(tmp_path / "missing" / "health.sock")
        )
    )
    assert readiness.reason is ListenerHealthReason.UNAVAILABLE
    assert readiness.observation_identity is None
