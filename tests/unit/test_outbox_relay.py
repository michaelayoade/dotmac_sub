"""Unit proof for the outbox relay dispatcher (SUB-2), without a database.

The SQL functions are exercised against PostgreSQL in
``tests/integration/test_outbox_relay_dispatcher_postgres.py``; here a fake
session records which settle the dispatcher asks for.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from dotmac_kernel.messaging.relay import ClaimedEvent, RelayPolicy

from app.outbox_relay import metrics
from app.outbox_relay.config import RelayConfig, RelayConfigError
from app.outbox_relay.dispatcher import run_cycle
from app.outbox_relay.health import heartbeat_is_fresh, write_heartbeat
from app.outbox_relay.preflight import (
    DispatcherObservation,
    dispatcher_identity_violations,
)
from app.outbox_relay.registry import DuplicateHandlerError, OutboxHandlerRegistry

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
TENANT = UUID("11111111-1111-4111-8111-111111111111")


@dataclass
class _Result:
    rows: list[dict[str, object]] = field(default_factory=list)
    value: object = True

    def mappings(self) -> _Result:
        return self

    def all(self) -> list[dict[str, object]]:
        return self.rows

    def scalar(self) -> object:
        return self.value


class FakeSession:
    def __init__(self, rows: list[dict[str, object]], lease_held: bool = True) -> None:
        self._rows = rows
        self._lease_held = lease_held
        self.settles: list[dict[str, object]] = []
        self.commits = 0

    def execute(self, statement: object, params: dict[str, object]) -> _Result:
        sql = str(statement)
        if "claim_outbox_batch" in sql:
            return _Result(rows=self._rows)
        assert "settle_outbox_event" in sql
        self.settles.append({"sql": sql, **params})
        return _Result(value=self._lease_held)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:  # pragma: no cover - not reached in these cases
        raise AssertionError("unexpected rollback")


def _row(event_type: str, attempts: int = 0) -> dict[str, object]:
    return {
        "id": uuid4(),
        "tenant_id": TENANT,
        "event_type": event_type,
        "payload": {},
        "attempts": attempts,
        "correlation_id": None,
        "created_at": NOW - timedelta(minutes=10),
    }


def _cycle(
    session: FakeSession, registry: OutboxHandlerRegistry, **kw: object
) -> metrics.CycleResult:
    return run_cycle(
        session,  # type: ignore[arg-type]
        registry=registry,
        policy=RelayPolicy(max_attempts=3),
        worker_id="w1",
        park_seconds=300,
        now=lambda: NOW,
        **kw,  # type: ignore[arg-type]
    )


def test_unregistered_event_type_is_parked_never_dead_lettered() -> None:
    session = FakeSession([_row("unknown.type", attempts=2)])
    result = _cycle(session, OutboxHandlerRegistry())

    assert (result.claimed, result.parked, result.failed, result.dead_lettered) == (
        1,
        1,
        0,
        0,
    )
    (settle,) = session.settles
    assert "'pending'" in str(settle["sql"])
    assert "'dead'" not in str(settle["sql"])
    assert settle["a"] == 2  # attempts untouched, even close to the max
    assert settle["ts"] == NOW + timedelta(seconds=300)
    assert result.oldest_pending_created_at == NOW - timedelta(minutes=10)


def test_registered_handler_success_settles_sent() -> None:
    seen: list[ClaimedEvent] = []
    registry = OutboxHandlerRegistry()
    registry.register("a.b", seen.append)
    session = FakeSession([_row("a.b")])
    result = _cycle(session, registry)

    assert result.succeeded == 1 and len(seen) == 1
    assert "'sent'" in str(session.settles[0]["sql"])
    assert result.oldest_pending_created_at is None


def test_handler_failure_backs_off_then_dead_letters_at_max_attempts() -> None:
    def boom(event: ClaimedEvent) -> None:
        raise RuntimeError("contains-private-payload")

    registry = OutboxHandlerRegistry()
    registry.register("a.b", boom)

    retry = _cycle(FakeSession([_row("a.b", attempts=0)]), registry)
    assert (retry.failed, retry.dead_lettered) == (1, 0)
    assert retry.oldest_pending_created_at is not None

    session = FakeSession([_row("a.b", attempts=2)])  # 3rd failure == max_attempts
    dead = _cycle(session, registry)
    assert (dead.failed, dead.dead_lettered) == (1, 1)
    assert "'dead'" in str(session.settles[0]["sql"])
    # only the exception class is persisted, never its message
    assert session.settles[0]["e"] == "RuntimeError"
    assert dead.oldest_pending_created_at is None


def test_lost_lease_is_counted_not_counted_as_success() -> None:
    registry = OutboxHandlerRegistry()
    registry.register("a.b", lambda event: None)
    result = _cycle(FakeSession([_row("a.b")], lease_held=False), registry)
    assert (result.succeeded, result.lease_lost) == (0, 1)


def test_one_handler_per_event_type() -> None:
    registry = OutboxHandlerRegistry()
    registry.register("a.b", lambda event: None)
    with pytest.raises(DuplicateHandlerError):
        registry.register("a.b", lambda event: None)
    with pytest.raises(ValueError):
        registry.register("  ", lambda event: None)


def test_shipped_registry_is_empty() -> None:
    from app.outbox_relay.registry import build_default_registry

    assert dict(build_default_registry().event_types) == {}


def test_record_cycle_updates_the_five_required_metrics() -> None:
    def value(counter: object) -> float:
        return float(counter._value.get())  # type: ignore[attr-defined]

    before = [
        value(m)
        for m in (
            metrics.CLAIMED,
            metrics.SUCCEEDED,
            metrics.FAILED,
            metrics.DEAD_LETTERED,
        )
    ]
    tracker = metrics.OldestPendingTracker(retain_seconds=600)
    metrics.record_cycle(
        metrics.CycleResult(
            claimed=4,
            succeeded=1,
            failed=2,
            dead_lettered=1,
            parked=0,
            oldest_pending_created_at=NOW - timedelta(seconds=90),
        ),
        tracker,
        NOW,
    )
    after = [
        value(m)
        for m in (
            metrics.CLAIMED,
            metrics.SUCCEEDED,
            metrics.FAILED,
            metrics.DEAD_LETTERED,
        )
    ]
    assert [b - a for a, b in zip(before, after, strict=True)] == [4, 1, 2, 1]
    assert metrics.OLDEST_PENDING_AGE._value.get() == 90.0  # type: ignore[attr-defined]


def test_oldest_pending_is_retained_then_released() -> None:
    tracker = metrics.OldestPendingTracker(retain_seconds=600)
    tracker.observe(NOW - timedelta(seconds=100), NOW)
    tracker.observe(None, NOW + timedelta(seconds=300))
    assert tracker.age_seconds(NOW + timedelta(seconds=300)) == 400.0
    tracker.observe(None, NOW + timedelta(seconds=601))
    assert tracker.age_seconds(NOW + timedelta(seconds=601)) == 0.0


def test_config_refuses_without_dedicated_url_and_hides_it() -> None:
    with pytest.raises(RelayConfigError, match="never falls back to DATABASE_URL"):
        RelayConfig.from_env({"DATABASE_URL": "postgresql://app:secret@h/db"})
    with pytest.raises(RelayConfigError):
        RelayConfig.from_env({"OUTBOX_RELAY_DATABASE_URL": "mysql://x"})
    with pytest.raises(RelayConfigError):
        RelayConfig.from_env(
            {
                "OUTBOX_RELAY_DATABASE_URL": "postgresql://u@h/d",
                "OUTBOX_RELAY_BATCH_SIZE": "0",
            }
        )
    config = RelayConfig.from_env(
        {"OUTBOX_RELAY_DATABASE_URL": "postgresql://u:topsecret@h/d"}
    )
    assert "topsecret" not in repr(config)
    assert (config.batch_size, config.max_attempts, config.metrics_port) == (
        50,
        8,
        9108,
    )


def _ok() -> DispatcherObservation:
    return DispatcherObservation(
        current_user="outbox_dispatcher",
        can_login=True,
        bypass_rls=False,
        superuser=False,
        relations_with_privilege=0,
        can_claim=True,
        can_settle=True,
        can_claim_platform=False,
        can_settle_platform=False,
    )


def test_preflight_accepts_only_the_execute_only_dispatcher() -> None:
    assert dispatcher_identity_violations(_ok()) == ()
    from dataclasses import replace

    for change, fragment in (
        ({"current_user": "dotmac_app"}, "not 'outbox_dispatcher'"),
        ({"bypass_rls": True}, "BYPASSRLS"),
        ({"superuser": True}, "SUPERUSER"),
        ({"relations_with_privilege": 1}, "table/column privileges"),
        ({"can_claim": False}, "claim_outbox_batch"),
        ({"can_settle": False}, "settle_outbox_event"),
        ({"can_claim_platform": True}, "platform-plane"),
    ):
        problems = dispatcher_identity_violations(replace(_ok(), **change))
        assert any(fragment in p for p in problems), (change, problems)


def test_heartbeat_freshness(tmp_path: Path) -> None:
    path = tmp_path / "hb"
    assert not heartbeat_is_fresh(path, max_age_seconds=60, now=1000.0)
    write_heartbeat(path)
    written = float(path.read_text().strip())
    assert heartbeat_is_fresh(path, max_age_seconds=60, now=written + 30)
    assert not heartbeat_is_fresh(path, max_age_seconds=60, now=written + 61)
    path.write_text("garbage")
    assert not heartbeat_is_fresh(path, max_age_seconds=60, now=written)
