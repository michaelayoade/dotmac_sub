"""Unit tests for the G4 cross-process OLT SSH session limiter (PR-A).

The Postgres advisory-lock layer is replaced by an in-memory lock server with
the same semantics (session locks owned by a connection, freed when the
connection closes), so priority sets, the intent lock, reentrancy and
release-exactly-once are exercised without a database. The real multi-process
proof against PostgreSQL lives in
``tests/integration/test_olt_ssh_limiter_multiprocess.py``.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from paramiko.ssh_exception import AuthenticationException, SSHException

from app.services.network import olt_ssh, olt_ssh_limiter
from app.services.network.olt_ssh_limiter import (
    LimiterConfig,
    OltSessionBusy,
    OltSshPriority,
    SlotLease,
    acquire_for_olt,
    acquire_slot,
    olt_ssh_priority,
    olt_ssh_scope,
    set_config_override,
)

_REAL_SLEEP = time.sleep


class _TimeShim:
    """The real ``time`` module with ``sleep`` replaced (module-local patch)."""

    def __init__(self, sleep) -> None:
        self.sleep = sleep

    def __getattr__(self, name: str):
        return getattr(time, name)


PG_URL = "postgresql+psycopg://user:pw@db.invalid:5432/example"  # documentation host


# ── In-memory advisory-lock server ──────────────────────────────────────────


class FakeLockServer:
    def __init__(self) -> None:
        self.mutex = threading.Lock()
        self.exclusive: dict[int, int] = {}
        self.shared: dict[int, set[int]] = {}
        self.next_conn = 0
        self.unlock_calls = 0
        self.connect_failure: Exception | None = None

    def held_exclusive(self) -> int:
        with self.mutex:
            return len(self.exclusive)

    def connect(self) -> FakeConn:
        if self.connect_failure is not None:
            raise self.connect_failure
        with self.mutex:
            self.next_conn += 1
            return FakeConn(self, self.next_conn)


class _Result:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar(self) -> object:
        return self._value


class FakeConn:
    def __init__(self, server: FakeLockServer, ident: int) -> None:
        self.server = server
        self.ident = ident
        self.closed = False

    def execute(self, statement: object, params: dict[str, int]) -> _Result:
        sql = str(statement)
        key = params["key"]
        s = self.server
        with s.mutex:
            if "pg_try_advisory_lock_shared" in sql:
                owner = s.exclusive.get(key)
                if owner is not None and owner != self.ident:
                    return _Result(False)
                s.shared.setdefault(key, set()).add(self.ident)
                return _Result(True)
            if "pg_try_advisory_lock" in sql:
                owner = s.exclusive.get(key)
                others = s.shared.get(key, set()) - {self.ident}
                if (owner is not None and owner != self.ident) or others:
                    return _Result(False)
                s.exclusive[key] = self.ident
                return _Result(True)
            if "pg_advisory_unlock_shared" in sql:
                held = self.ident in s.shared.get(key, set())
                s.shared.get(key, set()).discard(self.ident)
                return _Result(held)
            if "pg_advisory_unlock" in sql:
                s.unlock_calls += 1
                if s.exclusive.get(key) == self.ident:
                    del s.exclusive[key]
                    return _Result(True)
                return _Result(False)
        raise AssertionError(f"unexpected SQL: {sql}")

    def invalidate(self) -> None:
        self._free()

    def close(self) -> None:
        self._free()

    def _free(self) -> None:
        if self.closed:
            return
        self.closed = True
        with self.server.mutex:
            for key in [k for k, v in self.server.exclusive.items() if v == self.ident]:
                del self.server.exclusive[key]
            for holders in self.server.shared.values():
                holders.discard(self.ident)


@pytest.fixture
def server(monkeypatch) -> FakeLockServer:
    fake = FakeLockServer()
    monkeypatch.setattr(
        olt_ssh_limiter,
        "_limiter_engine",
        lambda url: SimpleNamespace(connect=fake.connect),
    )
    monkeypatch.setattr(olt_ssh_limiter, "_database_url", lambda: PG_URL)
    monkeypatch.setattr(
        olt_ssh_limiter, "time", _TimeShim(lambda s: _REAL_SLEEP(min(s, 0.02)))
    )
    set_config_override(LimiterConfig())
    return fake


def _olt(model: str = "MA5608T", name: str = "OLT-A") -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), name=name, model=model)


# ── Slot sets and priority ──────────────────────────────────────────────────


def test_slot_order_background_excludes_reserved_and_write_takes_reserved_first():
    assert olt_ssh_limiter._slot_order(OltSshPriority.BACKGROUND, 2, 1) == [0]
    assert olt_ssh_limiter._slot_order(OltSshPriority.BACKGROUND, 4, 1) == [0, 1, 2]
    assert olt_ssh_limiter._slot_order(OltSshPriority.WRITE, 2, 1) == [1, 0]
    assert olt_ssh_limiter._slot_order(OltSshPriority.INTERACTIVE, 2, 1) == [0, 1]
    # One slot total: background still gets it (nothing can be reserved).
    assert olt_ssh_limiter._slot_order(OltSshPriority.BACKGROUND, 1, 0) == [0]


def test_per_model_limits_and_reserved_clamp():
    config = LimiterConfig()
    assert config.limit_for(olt_ssh_limiter.model_key_for("MA5608T")) == 2
    assert config.limit_for(olt_ssh_limiter.model_key_for("Huawei MA5800-X7")) == 4
    assert config.limit_for(olt_ssh_limiter.model_key_for("something else")) == 2
    assert config.reserved_for(1) == 0  # never reserve every slot
    assert config.reserved_for(2) == 1
    assert LimiterConfig(reserved_priority_slots=9).reserved_for(4) == 3


def test_slot_keys_are_namespaced_and_distinct():
    olt = str(uuid.uuid4())
    keys = {olt_ssh_limiter.slot_key(olt, i) for i in range(4)}
    assert len(keys) == 4
    assert olt_ssh_limiter.intent_key(olt) not in keys
    # Not the bare-olt_id key the status poller lock uses.
    import hashlib

    bare = int.from_bytes(
        hashlib.blake2b(olt.encode("utf-8"), digest_size=8).digest(), "big", signed=True
    )
    assert bare not in keys


def test_limit_is_enforced_and_release_frees_a_slot(server):
    leases = [
        acquire_slot(
            olt_key="olt-1",
            limit=2,
            reserved=1,
            priority=OltSshPriority.WRITE,
            wait_seconds=0,
        )
        for _ in range(2)
    ]
    assert server.held_exclusive() == 2
    with pytest.raises(OltSessionBusy, match="olt_busy:"):
        acquire_slot(
            olt_key="olt-1",
            limit=2,
            reserved=1,
            priority=OltSshPriority.WRITE,
            wait_seconds=0,
        )
    leases[0].release()
    again = acquire_slot(
        olt_key="olt-1",
        limit=2,
        reserved=1,
        priority=OltSshPriority.WRITE,
        wait_seconds=0,
    )
    assert server.held_exclusive() == 2
    again.release()
    leases[1].release()
    assert server.held_exclusive() == 0


def test_other_olts_do_not_share_slots(server):
    a = acquire_slot(
        olt_key="olt-a",
        limit=1,
        reserved=0,
        priority=OltSshPriority.WRITE,
        wait_seconds=0,
    )
    b = acquire_slot(
        olt_key="olt-b",
        limit=1,
        reserved=0,
        priority=OltSshPriority.WRITE,
        wait_seconds=0,
    )
    a.release()
    b.release()


def test_background_never_takes_the_reserved_slot(server):
    first = acquire_slot(
        olt_key="olt-1",
        limit=2,
        reserved=1,
        priority=OltSshPriority.BACKGROUND,
        wait_seconds=0,
    )
    assert first.slot_index == 0
    with pytest.raises(OltSessionBusy):
        acquire_slot(
            olt_key="olt-1",
            limit=2,
            reserved=1,
            priority=OltSshPriority.BACKGROUND,
            wait_seconds=0,
        )
    # The reserved slot is still free for a writer.
    writer = acquire_slot(
        olt_key="olt-1",
        limit=2,
        reserved=1,
        priority=OltSshPriority.WRITE,
        wait_seconds=0,
    )
    assert writer.slot_index == 1
    writer.release()
    first.release()


def test_background_never_waits_it_skips(server):
    held = acquire_slot(
        olt_key="olt-1",
        limit=1,
        reserved=0,
        priority=OltSshPriority.WRITE,
        wait_seconds=0,
    )
    started = time.monotonic()
    with pytest.raises(OltSessionBusy):
        acquire_slot(
            olt_key="olt-1",
            limit=1,
            reserved=0,
            priority=OltSshPriority.BACKGROUND,
            wait_seconds=60,  # ignored for background
        )
    assert time.monotonic() - started < 1.0
    held.release()


def test_background_skips_while_a_writer_is_queued(server):
    held = acquire_slot(
        olt_key="olt-1",
        limit=1,
        reserved=0,
        priority=OltSshPriority.WRITE,
        wait_seconds=0,
    )
    got: dict[str, SlotLease] = {}

    def writer() -> None:
        got["lease"] = acquire_slot(
            olt_key="olt-1",
            limit=1,
            reserved=0,
            priority=OltSshPriority.WRITE,
            wait_seconds=10,
        )

    thread = threading.Thread(target=writer)
    thread.start()
    # Wait until the writer has registered its intent (it holds a shared lock).
    intent = olt_ssh_limiter.intent_key("olt-1")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not server.shared.get(intent):
        time.sleep(0.01)
    assert server.shared.get(intent), "writer never registered intent"

    with pytest.raises(OltSessionBusy, match="higher-priority"):
        acquire_slot(
            olt_key="olt-1",
            limit=1,
            reserved=0,
            priority=OltSshPriority.BACKGROUND,
            wait_seconds=0,
        )
    held.release()
    thread.join(timeout=5)
    assert "lease" in got
    # Intent is dropped once the writer holds its slot.
    assert not server.shared.get(intent)
    got["lease"].release()


def test_release_is_idempotent_and_unlocks_once(server):
    lease = acquire_slot(
        olt_key="olt-1",
        limit=1,
        reserved=0,
        priority=OltSshPriority.WRITE,
        wait_seconds=0,
    )
    lease.release()
    lease.release()
    assert server.unlock_calls == 1
    assert server.held_exclusive() == 0


def test_failed_unlock_invalidates_the_connection(server, monkeypatch):
    lease = acquire_slot(
        olt_key="olt-1",
        limit=1,
        reserved=0,
        priority=OltSshPriority.WRITE,
        wait_seconds=0,
    )
    conn = lease._conn
    monkeypatch.setattr(
        conn, "execute", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    lease.release()  # must not raise
    assert conn.closed is True
    assert server.held_exclusive() == 0


def test_database_down_fails_closed(server):
    server.connect_failure = RuntimeError("connection refused")
    with pytest.raises(OltSessionBusy, match="limiter database unavailable"):
        acquire_slot(
            olt_key="olt-1",
            limit=2,
            reserved=1,
            priority=OltSshPriority.WRITE,
            wait_seconds=0,
        )


def test_non_postgres_database_is_a_no_op(monkeypatch):
    monkeypatch.setattr(olt_ssh_limiter, "_database_url", lambda: "sqlite://")
    lease = acquire_slot(
        olt_key="x", limit=1, reserved=0, priority=OltSshPriority.WRITE, wait_seconds=0
    )
    assert lease.is_null
    lease.release()


def test_off_switch_returns_unlimited_null_lease(server):
    set_config_override(LimiterConfig(enabled=False))
    leases = [acquire_for_olt(_olt()) for _ in range(5)]
    assert server.next_conn == 0  # never touched the lock database
    for lease in leases:
        lease.release()


# ── Priority wiring, budgets, reentrancy ────────────────────────────────────


def test_wait_budgets_follow_priority_and_deadline(server, monkeypatch):
    seen: list[tuple[OltSshPriority, float]] = []

    def fake_acquire(**kwargs):
        seen.append((kwargs["priority"], kwargs["wait_seconds"]))
        return SlotLease.null(kwargs["olt_key"])

    monkeypatch.setattr(olt_ssh_limiter, "acquire_slot", fake_acquire)
    olt = _olt()

    def take() -> None:
        olt_ssh_limiter.mark_released(acquire_for_olt(olt))

    with olt_ssh_priority(OltSshPriority.WRITE):
        take()
    with olt_ssh_priority(OltSshPriority.INTERACTIVE):
        take()
    with olt_ssh_priority(OltSshPriority.BACKGROUND):
        take()
    # Writer capped by the reconcile deadline minus 30 s of working time.
    with olt_ssh_priority(
        OltSshPriority.WRITE, deadline_monotonic=time.monotonic() + 50
    ):
        take()
    assert [p for p, _ in seen] == [
        OltSshPriority.WRITE,
        OltSshPriority.INTERACTIVE,
        OltSshPriority.BACKGROUND,
        OltSshPriority.WRITE,
    ]
    assert seen[0][1] == 45
    assert seen[1][1] == 15
    assert seen[2][1] == 0
    assert 15 < seen[3][1] <= 20


def test_default_priority_is_interactive():
    assert olt_ssh_limiter.current_priority() is OltSshPriority.INTERACTIVE


def test_nested_open_never_blocks_while_holding_a_slot(server):
    olt = _olt()
    set_config_override(LimiterConfig(max_ma5608t=1))
    outer = acquire_for_olt(olt)
    started = time.monotonic()
    with olt_ssh_priority(OltSshPriority.WRITE):
        with pytest.raises(OltSessionBusy):
            acquire_for_olt(olt)  # would wait 45 s if it were allowed to
    assert time.monotonic() - started < 1.0
    olt_ssh_limiter.mark_released(outer)
    # Holding marker is gone again: a normal open waits/acquires normally.
    again = acquire_for_olt(olt)
    olt_ssh_limiter.mark_released(again)


def test_reconcile_budget_fits_inside_the_task_soft_limit():
    """Source-level check (no app import): the slot wait plus the EOF retry sit
    inside the reconcile deadline, which sits inside the task soft limit."""
    root = Path(__file__).resolve().parents[1] / "app"
    service = (root / "services/network/ont_service_configuration.py").read_text()
    reconcile_timeout = int(
        re.search(
            r"^SERVICE_CONFIGURATION_RECONCILE_TIMEOUT_SECONDS = (\d+)", service, re.M
        ).group(1)
    )
    tasks = (root / "tasks/ont_service_configuration.py").read_text()
    soft = min(int(v) for v in re.findall(r"soft_time_limit=(\d+)", tasks))
    assert reconcile_timeout + 20 <= soft
    config = LimiterConfig()
    # Writer wait is additionally capped at (deadline - 30 s) at run time.
    assert config.wait_write_seconds < reconcile_timeout - 30
    assert config.auth_eof_retry_delay_seconds + 25 < reconcile_timeout


# ── Scope: one slot, one login per pass ─────────────────────────────────────


class FakeChannel:
    """Scripted Huawei shell: each ``send`` queues the next scripted reply."""

    def __init__(self, replies: list[str] | None = None) -> None:
        self.sent: list[str] = []
        self.replies = list(replies or [])
        self.pending: list[bytes] = []
        self.closed = False

    def settimeout(self, _value: float) -> None:
        return None

    def get_pty(self, **_kw) -> None:
        return None

    def invoke_shell(self) -> None:
        return None

    def send(self, data: str) -> None:
        self.sent.append(data)
        if self.replies:
            reply = self.replies.pop(0)
            self.pending.append(reply.encode() if reply is not None else b"")

    def recv(self, _n: int) -> bytes:
        if self.pending:
            return self.pending.pop(0)
        raise TimeoutError


class FakeSessionTransport:
    def __init__(self, channel: FakeChannel) -> None:
        self.channel = channel
        self.closed_real = False
        self.closed = False

    def is_active(self) -> bool:
        return not self.closed_real and not self.closed

    def session_clean(self) -> bool:
        return olt_ssh._channel_is_clean(self.channel)

    def close_real(self) -> None:
        self.closed_real = True

    def close(self) -> None:
        self.closed = True


def _clean_session(opens: list[FakeSessionTransport], prompt: str = "boi-olt>"):
    channel = FakeChannel(replies=[])
    olt_ssh._instrument_channel(channel)
    channel.pending.append(prompt.encode())
    channel.recv(4096)  # consume the login prompt: channel is now clean
    transport = FakeSessionTransport(channel)
    opens.append(transport)
    return transport, channel, SimpleNamespace(prompt_regex=r"\S*>\s*$")


def test_scope_reuses_one_session_for_a_whole_pass(server, monkeypatch):
    opens: list[FakeSessionTransport] = []
    monkeypatch.setattr(
        olt_ssh,
        "_connect_unlimited",
        lambda olt, **kw: _clean_session(opens),
    )
    set_config_override(LimiterConfig(max_ma5608t=1, release_grace_seconds_ma5608t=0))
    olt = _olt()
    with olt_ssh_scope(olt, OltSshPriority.WRITE):
        # pre-apply reads, apply, verify re-read: five separate "logins".
        for _ in range(5):
            transport, channel, _policy = olt_ssh._open_shell(olt)
            assert server.held_exclusive() == 1  # the scope's one slot
            transport.close()  # checks back in; does not close the session
    assert len(opens) == 1, "a reconcile pass must log in exactly once"
    assert opens[0].closed_real is True  # closed when the scope ended
    assert server.held_exclusive() == 0


def test_scope_busy_raises_before_any_connect(server, monkeypatch):
    connects: list[object] = []
    monkeypatch.setattr(
        olt_ssh, "_connect_unlimited", lambda olt, **kw: connects.append(1)
    )
    set_config_override(LimiterConfig(max_ma5608t=1))
    olt = _olt()
    holder = acquire_for_olt(olt)
    with olt_ssh_priority(OltSshPriority.WRITE):
        with pytest.raises(OltSessionBusy, match="olt_busy:"):
            with olt_ssh_scope(
                olt, OltSshPriority.WRITE, deadline_monotonic=time.monotonic() + 31
            ):
                pytest.fail("scope must not be entered")
    assert connects == []
    olt_ssh_limiter.mark_released(holder)


def test_scope_discards_a_dirty_session_and_reopens_in_the_same_slot(
    server, monkeypatch
):
    opens: list[FakeSessionTransport] = []
    monkeypatch.setattr(
        olt_ssh, "_connect_unlimited", lambda olt, **kw: _clean_session(opens)
    )
    set_config_override(LimiterConfig(max_ma5608t=1, release_grace_seconds_ma5608t=0))
    olt = _olt()
    with olt_ssh_scope(olt, OltSshPriority.WRITE):
        transport, channel, _ = olt_ssh._open_shell(olt)
        channel.send("display service-port all\n")  # sent, never read: dirty
        transport.close()
        assert opens[0].closed_real is True  # discarded at check-in
        olt_ssh._open_shell(olt)[0].close()
    assert len(opens) == 2
    assert server.held_exclusive() == 0


def test_scope_returns_a_privileged_session_to_user_mode_before_reuse(
    server, monkeypatch
):
    opens: list[FakeSessionTransport] = []

    def opener(olt, **kw):
        transport, channel, policy = _clean_session(opens)
        return transport, channel, policy

    monkeypatch.setattr(olt_ssh, "_connect_unlimited", opener)
    set_config_override(LimiterConfig(max_ma5608t=1, release_grace_seconds_ma5608t=0))
    olt = _olt()
    with olt_ssh_scope(olt, OltSshPriority.WRITE):
        transport, channel, _ = olt_ssh._open_shell(olt)
        channel.send("enable\n")
        channel.pending.append(b"\r\nboi-olt#")
        channel.recv(4096)  # privileged prompt, clean
        channel.replies = ["\r\nboi-olt>"]  # reply to the normalising `quit`
        transport.close()
        olt_ssh._open_shell(olt)[0].close()
        assert channel.sent[-1] == "quit\n"
    assert len(opens) == 1


def test_failed_open_releases_the_slot(server, monkeypatch):
    def boom(olt, **kw):
        raise SSHException("Error reading SSH protocol banner")

    monkeypatch.setattr(olt_ssh, "_connect_unlimited", boom)
    olt = _olt()
    with pytest.raises(SSHException):
        olt_ssh._open_shell(olt)
    assert server.held_exclusive() == 0


def test_open_shell_busy_is_raised_before_any_socket(server, monkeypatch):
    monkeypatch.setattr(
        olt_ssh.socket,
        "create_connection",
        lambda *a, **k: pytest.fail("socket opened while the OLT is busy"),
    )
    set_config_override(LimiterConfig(max_ma5608t=1))
    olt = _olt()
    holder = acquire_for_olt(olt)
    with olt_ssh_priority(OltSshPriority.BACKGROUND):
        with pytest.raises(OltSessionBusy) as excinfo:
            olt_ssh._open_shell(olt)
    assert isinstance(excinfo.value, TimeoutError)
    assert str(excinfo.value).startswith("olt_busy:")
    olt_ssh_limiter.mark_released(holder)


# ── Logout hygiene ──────────────────────────────────────────────────────────


class FakeLease:
    def __init__(self) -> None:
        self.releases = 0
        self.wait_seconds = 0.0
        self._holding_token = None

    def release(self) -> None:
        self.releases += 1


def _managed(channel: FakeChannel, *, logout: bool = True):
    olt_ssh._instrument_channel(channel)
    closes: list[str] = []
    transport = SimpleNamespace(
        close=lambda: closes.append("close"), is_active=lambda: True
    )
    lease = FakeLease()
    olt_ssh._manage_transport(
        transport,
        lease=lease,
        logout_on_close=logout,
        grace_seconds=0,
        olt_label="OLT-A",
        priority="write",
        opened_at=time.monotonic(),
    )
    olt_ssh._attach_channel(transport, channel)
    return transport, lease, closes


def _make_clean(channel: FakeChannel, prompt: str) -> None:
    channel.pending.append(prompt.encode())
    channel.recv(4096)


def test_logout_from_clean_prompt_confirms_only_the_logout_question():
    channel = FakeChannel(
        replies=["\r\nboi-olt>", "Are you sure to log out? (y/n)[n]:", ""]
    )
    transport, lease, closes = _managed(channel)
    _make_clean(channel, "boi-olt#")
    transport.close()
    assert channel.sent == ["quit\n", "quit\n", "y\n"]
    assert closes == ["close"]
    assert lease.releases == 1


def test_a_foreign_confirmation_is_never_answered():
    channel = FakeChannel(replies=["Are you sure to delete the ONT? (y/n)[n]:"])
    transport, lease, closes = _managed(channel)
    _make_clean(channel, "boi-olt(config)#")
    transport.close()
    assert "y\n" not in channel.sent
    assert channel.sent == ["quit\n"]
    assert closes == ["close"] and lease.releases == 1


def test_dirty_session_is_closed_without_typing_anything():
    channel = FakeChannel()
    transport, lease, closes = _managed(channel)
    _make_clean(channel, "boi-olt#")
    channel.send("display current-configuration\n")  # sent, output not read
    sent_before = list(channel.sent)
    transport.close()
    assert channel.sent == sent_before
    assert closes == ["close"] and lease.releases == 1


def test_mid_pager_session_is_never_typed_into():
    channel = FakeChannel()
    transport, lease, closes = _managed(channel)
    _make_clean(channel, "---- More ( Press 'Q' to break ) ----")
    transport.close()
    assert channel.sent == []
    assert closes == ["close"] and lease.releases == 1


def test_logout_disabled_by_setting():
    channel = FakeChannel()
    transport, lease, _ = _managed(channel, logout=False)
    _make_clean(channel, "boi-olt>")
    transport.close()
    assert channel.sent == []
    assert lease.releases == 1


def test_close_is_idempotent_and_releases_exactly_once():
    channel = FakeChannel(replies=["Are you sure to log out? (y/n)[n]:", ""])
    transport, lease, closes = _managed(channel)
    _make_clean(channel, "boi-olt>")
    transport.close()
    transport.close()
    assert closes == ["close"]
    assert lease.releases == 1
    assert channel.sent.count("y\n") == 1


def test_real_slot_lease_survives_double_release_from_close_paths(server):
    lease = acquire_slot(
        olt_key="olt-1",
        limit=1,
        reserved=0,
        priority=OltSshPriority.WRITE,
        wait_seconds=0,
    )
    olt_ssh_limiter.mark_released(lease)
    olt_ssh_limiter.mark_released(lease)
    assert server.unlock_calls == 1


def test_close_never_raises_when_the_channel_blows_up():
    channel = FakeChannel()
    transport, lease, closes = _managed(channel)
    _make_clean(channel, "boi-olt>")
    channel._olt_raw_send = lambda _data: (_ for _ in ()).throw(OSError("reset"))
    transport.close()
    assert closes == ["close"] and lease.releases == 1


# ── Auth-phase EOF retry ────────────────────────────────────────────────────


class ScriptedTransport:
    script: list[BaseException | None] = []
    created = 0

    def __init__(self, _sock) -> None:
        type(self).created += 1
        self.channel = FakeChannel()

    def start_client(self, timeout: int) -> None:
        item = type(self).script.pop(0) if type(self).script else None
        if item is not None:
            raise item

    def auth_password(self, **_kw) -> None:
        return None

    def is_authenticated(self) -> bool:
        return True

    def open_session(self, timeout: int):
        return self.channel

    def is_active(self) -> bool:
        return True

    def close(self) -> None:
        return None


@pytest.fixture
def connect_env(monkeypatch):
    sleeps: list[float] = []
    ScriptedTransport.script = []
    ScriptedTransport.created = 0
    monkeypatch.setattr(olt_ssh, "Transport", ScriptedTransport)
    monkeypatch.setattr(olt_ssh.socket, "create_connection", lambda *a, **k: object())
    monkeypatch.setattr(
        olt_ssh,
        "resolve_policy",
        lambda _o: SimpleNamespace(prompt_regex=r"\S*[>#]\s*$"),
    )
    monkeypatch.setattr(olt_ssh, "decrypt_credential", lambda *_a, **_k: "pw")
    monkeypatch.setattr(olt_ssh, "_apply_preferred_algorithms", lambda *a, **k: None)
    monkeypatch.setattr(olt_ssh, "_prime_shell_prompt", lambda ch, rx: "boi-olt>")
    monkeypatch.setattr(olt_ssh, "time", _TimeShim(sleeps.append))
    set_config_override(LimiterConfig(enabled=False))
    return sleeps


def _device() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="OLT-A",
        model="MA5608T",
        mgmt_ip="192.0.2.10",
        hostname=None,
        ssh_port=22,
        ssh_username="u",
        ssh_password="enc",
    )


def test_one_retry_on_eof_before_a_channel_exists(connect_env):
    ScriptedTransport.script = [SSHException("Error reading SSH protocol banner")]
    transport, channel, _ = olt_ssh._connect_unlimited(
        _device(), lease=None, priority_name="write"
    )
    assert ScriptedTransport.created == 2
    assert len(connect_env) == 1 and 4 <= connect_env[0] <= 10  # 7 s +/- jitter


def test_only_one_retry_never_two(connect_env):
    ScriptedTransport.script = [
        SSHException("Error reading SSH protocol banner"),
        SSHException("Error reading SSH protocol banner"),
    ]
    with pytest.raises(SSHException):
        olt_ssh._connect_unlimited(_device(), lease=None, priority_name="write")
    assert ScriptedTransport.created == 2


def test_password_reject_is_never_retried(connect_env):
    ScriptedTransport.script = [AuthenticationException("Authentication failed.")]
    with pytest.raises(AuthenticationException):
        olt_ssh._connect_unlimited(_device(), lease=None, priority_name="write")
    assert ScriptedTransport.created == 1 and connect_env == []


def test_background_never_retries(connect_env):
    ScriptedTransport.script = [SSHException("Error reading SSH protocol banner")]
    with pytest.raises(SSHException):
        olt_ssh._connect_unlimited(_device(), lease=None, priority_name="background")
    assert ScriptedTransport.created == 1


def test_retry_delay_zero_disables_the_retry(connect_env):
    set_config_override(LimiterConfig(enabled=False, auth_eof_retry_delay_seconds=0))
    ScriptedTransport.script = [SSHException("transport shut down or saw EOF")]
    with pytest.raises(SSHException):
        olt_ssh._connect_unlimited(_device(), lease=None, priority_name="write")
    assert ScriptedTransport.created == 1


def test_retry_is_skipped_when_the_deadline_cannot_fit_it(connect_env):
    ScriptedTransport.script = [SSHException("Error reading SSH protocol banner")]
    with olt_ssh_priority_ctx(deadline=time.monotonic() + 10):
        with pytest.raises(SSHException):
            olt_ssh._connect_unlimited(_device(), lease=None, priority_name="write")
    assert ScriptedTransport.created == 1


def olt_ssh_priority_ctx(*, deadline: float):
    return olt_ssh_priority(OltSshPriority.WRITE, deadline_monotonic=deadline)


def test_post_shell_failures_are_not_retried(connect_env, monkeypatch):
    def broken_prime(_channel, _regex):
        raise SSHException("transport shut down or saw EOF")

    monkeypatch.setattr(olt_ssh, "_prime_shell_prompt", broken_prime)
    with pytest.raises(SSHException):
        olt_ssh._connect_unlimited(_device(), lease=None, priority_name="write")
    assert ScriptedTransport.created == 1


# ── Single transport factory ────────────────────────────────────────────────


def test_open_shell_is_the_only_olt_transport_factory():
    """Every OLT SSH session goes through ``olt_ssh._open_shell``, so the
    limiter, logout and slot release apply to every call site (including the
    scheduled observation refresh) without editing them."""
    root = Path(__file__).resolve().parents[1] / "app"
    pattern = re.compile(r"(?<![A-Za-z_])Transport\(")
    offenders = [
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if pattern.search(path.read_text(encoding="utf-8"))
        and "paramiko" in path.read_text(encoding="utf-8")
    ]
    assert offenders == ["services/network/olt_ssh.py"]
    assert "SSHClient(" not in (root / "services/network/olt_ssh.py").read_text(
        encoding="utf-8"
    )


# ── Metrics helpers ─────────────────────────────────────────────────────────


def test_lock_halves_match_how_postgres_reports_bigint_advisory_keys():
    from app.services.network.olt_ssh_metrics import _lock_halves

    assert _lock_halves(1) == (0, 1)
    assert _lock_halves(-1) == (0xFFFFFFFF, 0xFFFFFFFF)
    assert _lock_halves((5 << 32) | 7) == (5, 7)
    assert _lock_halves(-(2**63)) == (0x80000000, 0)


def test_open_failures_are_classified_into_the_bounded_outcome_set():
    from app.services.network.olt_ssh_metrics import (
        OPEN_OUTCOMES,
        classify_open_failure,
    )

    cases = {
        "busy_timeout": OltSessionBusy("olt_busy: x"),
        "auth_rejected": AuthenticationException("Authentication failed."),
        "auth_eof": SSHException("Error reading SSH protocol banner"),
        "connect_timeout": TimeoutError("timed out"),
        "other": ValueError("bad"),
    }
    for expected, exc in cases.items():
        assert classify_open_failure(exc) == expected
        assert expected in OPEN_OUTCOMES


def test_wait_and_open_counters_are_recorded_fail_soft(monkeypatch):
    from app.services.network import olt_ssh_metrics

    recorded: list[tuple[str, float]] = []
    monkeypatch.setattr(
        olt_ssh_metrics,
        "_incr",
        lambda field, amount=1: recorded.append((field, amount)),
    )
    olt_ssh_metrics.record_open("OLT-A", "write", "ok")
    olt_ssh_metrics.record_wait("OLT-A", "write", 2.0)
    olt_ssh_metrics.record_logout("clean")

    fields = [f for f, _ in recorded]
    assert "open|OLT-A|write|ok" in fields
    assert "logout|clean" in fields
    assert "wait_bucket|OLT-A|write|5.0" in fields
    assert "wait_bucket|OLT-A|write|0.5" not in fields  # 2.0 s is above 0.5
    assert "wait_bucket|OLT-A|write|+Inf" in fields
    assert ("wait_sum|OLT-A|write", 2.0) in recorded


def test_metrics_never_raise_into_the_ssh_path(monkeypatch):
    from app.services.network import olt_ssh_metrics

    class Boom:
        def hincrby(self, *a, **k):
            raise ConnectionError("redis down")

        hincrbyfloat = expire = hincrby

    monkeypatch.setattr(olt_ssh_metrics, "_redis", lambda: Boom())
    monkeypatch.setattr(olt_ssh_metrics, "_redis_down_until", 0.0)
    olt_ssh_metrics.record_open("OLT-A", "write", "ok")  # must not raise


def test_snapshot_publishes_held_slots_and_counters_within_the_registered_bound(
    monkeypatch,
):
    from app.services import observability
    from app.services.network import olt_ssh_metrics

    olts = [
        SimpleNamespace(id=uuid.uuid4(), name=f"OLT {i}/ Gudu", model="MA5608T")
        for i in range(20)  # more than the 14 the producer is bounded to
    ]
    held_names = {olts[0].name: (2, 41.0)}

    class FakeResult:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

    class FakeDb:
        def execute(self, *_a, **_k):
            return FakeResult(olts[:14])  # the producer's LIMIT 14

    monkeypatch.setattr(
        olt_ssh_metrics, "held_slots_by_olt", lambda db, o, c: dict(held_names)
    )
    monkeypatch.setattr(
        olt_ssh_metrics,
        "load_counters",
        lambda: {
            f"open|{olts[0].name}|write|ok": 7.0,
            f"open|{olts[0].name}|background|auth_eof": 3.0,
            f"wait_sum|{olts[0].name}|write": 12.5,
            f"wait_bucket|{olts[0].name}|write|+Inf": 4.0,
            "logout|clean": 9.0,
        },
    )
    published: dict[str, object] = {}

    def fake_publish(domain, observations, **_kw):
        published["domain"] = domain
        published["observations"] = list(observations)
        return True

    monkeypatch.setattr(observability, "publish_state_snapshot", fake_publish)

    assert olt_ssh_metrics.publish_olt_ssh_snapshot(FakeDb()) is True

    observations = published["observations"]
    by_key = {(o.signal, o.scope): o.value for o in observations}
    scope = olt_ssh_metrics._scope(olts[0].name)
    assert re.fullmatch(r"[A-Za-z0-9_.:-]+", scope)
    assert published["domain"] == "olt_ssh"
    assert by_key[("sessions_held", scope)] == 2.0
    assert by_key[("oldest_hold_seconds", scope)] == 41.0
    assert by_key[("open_ok", scope)] == 7.0
    assert by_key[("open_auth_eof", scope)] == 3.0
    assert by_key[("slot_wait_seconds_sum", scope)] == 12.5
    assert by_key[("slot_wait_count", scope)] == 4.0
    assert by_key[("logout_clean", "all")] == 9.0
    spec = observability._STATE_SNAPSHOT_SPECS["olt_ssh"]
    assert len(observations) <= spec["max_observations"]
