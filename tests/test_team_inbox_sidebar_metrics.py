from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from app.services import (
    team_inbox_operations,
    team_inbox_read,
    team_inbox_sidebar_metrics,
)


@pytest.fixture(autouse=True)
def _reset_local_cache_state(monkeypatch) -> None:
    monkeypatch.setattr(team_inbox_sidebar_metrics, "_fallback_snapshot", None)
    monkeypatch.setattr(team_inbox_sidebar_metrics, "_fallback_retry_after", None)
    monkeypatch.setattr(
        team_inbox_sidebar_metrics, "_assignment_fallback_snapshots", {}
    )


def test_inbox_read_budget_is_below_the_browser_deadline(monkeypatch) -> None:
    db = MagicMock()
    db.get_bind.return_value.dialect.name = "postgresql"
    monkeypatch.setattr(
        team_inbox_sidebar_metrics,
        "settings",
        SimpleNamespace(inbox_read_statement_timeout_ms=8000),
    )

    team_inbox_sidebar_metrics.apply_read_budget(db)

    statement, parameters = db.execute.call_args.args
    assert "set_config('statement_timeout'" in str(statement)
    assert parameters == {"timeout": "8000ms"}


def test_inbox_read_budget_is_not_applied_to_sqlite() -> None:
    db = MagicMock()
    db.get_bind.return_value.dialect.name = "sqlite"

    team_inbox_sidebar_metrics.apply_read_budget(db)

    db.execute.assert_not_called()


class _LeaseClient:
    def __init__(self, claimed: bool) -> None:
        self.claimed = claimed
        self.released = False

    def set(self, _name: str, _value: str, *, nx: bool, ex: int) -> bool:
        assert nx is True
        assert ex == team_inbox_sidebar_metrics.REFRESH_LEASE_SECONDS
        return self.claimed

    def eval(self, _script: str, _numkeys: int, *_args: str) -> int:
        self.released = True
        return 1


def _queue_metrics(value: int) -> team_inbox_operations.InboxQueueMetrics:
    return team_inbox_operations.InboxQueueMetrics(
        total_open=value,
        needs_response=value + 1,
        failed_outbound=value + 2,
        unassigned_open=value + 3,
        muted_open=value + 4,
        snoozed_open=value + 5,
    )


def _payload(observed_at: datetime, value: int = 10) -> dict[str, object]:
    return {
        "queue_metrics": {
            "total_open": value,
            "needs_response": value + 1,
            "failed_outbound": value + 2,
            "unassigned_open": value + 3,
            "muted_open": value + 4,
            "snoozed_open": value + 5,
        },
        "needs_attention": value + 6,
        "observed_at": observed_at.isoformat(),
    }


def test_fresh_local_snapshot_skips_redis_connection(monkeypatch) -> None:
    now = datetime(2026, 11, 5, 9, 30, tzinfo=UTC)
    local = team_inbox_sidebar_metrics.InboxSidebarMetricsSnapshot(
        queue_metrics=_queue_metrics(10),
        needs_attention=16,
        observed_at=now - timedelta(seconds=5),
    )
    monkeypatch.setattr(team_inbox_sidebar_metrics, "_fallback_snapshot", local)
    monkeypatch.setattr(
        team_inbox_sidebar_metrics.app_cache,
        "get_cache_redis",
        lambda: (_ for _ in ()).throw(AssertionError("unexpected Redis connection")),
    )

    snapshot = team_inbox_sidebar_metrics.get_sidebar_metrics(object(), now=now)

    assert snapshot is local


def test_fresh_shared_snapshot_skips_database_recalculation(monkeypatch) -> None:
    now = datetime(2026, 11, 5, 9, 30, tzinfo=UTC)
    monkeypatch.setattr(team_inbox_sidebar_metrics, "_fallback_snapshot", None)
    monkeypatch.setattr(
        team_inbox_sidebar_metrics.app_cache,
        "get_json",
        lambda _key: _payload(now - timedelta(seconds=5)),
    )
    monkeypatch.setattr(
        team_inbox_sidebar_metrics.app_cache,
        "get_cache_redis",
        lambda: _LeaseClient(claimed=False),
    )
    monkeypatch.setattr(
        team_inbox_sidebar_metrics,
        "_compute_snapshot",
        lambda _db, _now: (_ for _ in ()).throw(AssertionError("unexpected compute")),
    )

    snapshot = team_inbox_sidebar_metrics.get_sidebar_metrics(object(), now=now)

    assert snapshot.queue_metrics.total_open == 10
    assert snapshot.needs_attention == 16


def test_busy_refresh_lease_serves_stale_snapshot(monkeypatch) -> None:
    now = datetime(2026, 11, 5, 9, 30, tzinfo=UTC)
    client = _LeaseClient(claimed=False)
    monkeypatch.setattr(team_inbox_sidebar_metrics, "_fallback_snapshot", None)
    monkeypatch.setattr(
        team_inbox_sidebar_metrics.app_cache,
        "get_json",
        lambda _key: _payload(now - timedelta(seconds=30)),
    )
    monkeypatch.setattr(
        team_inbox_sidebar_metrics.app_cache,
        "get_cache_redis",
        lambda: client,
    )

    snapshot = team_inbox_sidebar_metrics.get_sidebar_metrics(object(), now=now)

    assert snapshot.queue_metrics.total_open == 10
    assert client.released is False


def test_refresh_lease_has_one_producer_and_records_last_good_snapshot(
    monkeypatch,
) -> None:
    now = datetime(2026, 11, 5, 9, 30, tzinfo=UTC)
    client = _LeaseClient(claimed=True)
    stored: list[tuple[object, int]] = []
    computed = team_inbox_sidebar_metrics.InboxSidebarMetricsSnapshot(
        queue_metrics=_queue_metrics(20),
        needs_attention=26,
        observed_at=now,
    )
    monkeypatch.setattr(team_inbox_sidebar_metrics, "_fallback_snapshot", None)
    monkeypatch.setattr(
        team_inbox_sidebar_metrics.app_cache, "get_json", lambda _key: None
    )
    monkeypatch.setattr(
        team_inbox_sidebar_metrics.app_cache,
        "get_cache_redis",
        lambda: client,
    )
    monkeypatch.setattr(
        team_inbox_sidebar_metrics,
        "_compute_snapshot",
        lambda _db, _now: computed,
    )
    monkeypatch.setattr(
        team_inbox_sidebar_metrics.app_cache,
        "set_json",
        lambda _key, value, ttl_seconds: stored.append((value, ttl_seconds)) or True,
    )

    snapshot = team_inbox_sidebar_metrics.get_sidebar_metrics(object(), now=now)

    assert snapshot == computed
    assert stored[0][1] == team_inbox_sidebar_metrics.MAX_STALE_SECONDS
    assert client.released is True


def test_busy_cold_cache_fails_open_without_database_work(monkeypatch) -> None:
    now = datetime(2026, 11, 5, 9, 30, tzinfo=UTC)
    monkeypatch.setattr(team_inbox_sidebar_metrics, "_fallback_snapshot", None)
    monkeypatch.setattr(
        team_inbox_sidebar_metrics.app_cache, "get_json", lambda _key: None
    )
    monkeypatch.setattr(
        team_inbox_sidebar_metrics.app_cache,
        "get_cache_redis",
        lambda: _LeaseClient(claimed=False),
    )

    snapshot = team_inbox_sidebar_metrics.get_sidebar_metrics(object(), now=now)

    assert snapshot.queue_metrics.total_open == 0
    assert snapshot.needs_attention == 0


def test_failed_producer_keeps_the_bounded_redis_circuit_breaker(monkeypatch) -> None:
    now = datetime(2026, 11, 5, 9, 30, tzinfo=UTC)
    client = _LeaseClient(claimed=True)
    monkeypatch.setattr(team_inbox_sidebar_metrics, "_fallback_snapshot", None)
    monkeypatch.setattr(
        team_inbox_sidebar_metrics.app_cache, "get_json", lambda _key: None
    )
    monkeypatch.setattr(
        team_inbox_sidebar_metrics.app_cache,
        "get_cache_redis",
        lambda: client,
    )
    monkeypatch.setattr(
        team_inbox_sidebar_metrics,
        "_compute_snapshot",
        lambda _db, _now: (_ for _ in ()).throw(RuntimeError("query failed")),
    )

    with pytest.raises(RuntimeError, match="query failed"):
        team_inbox_sidebar_metrics.get_sidebar_metrics(object(), now=now)

    assert client.released is False


def test_failed_local_producer_opens_a_bounded_circuit_breaker(monkeypatch) -> None:
    now = datetime(2026, 11, 5, 9, 30, tzinfo=UTC)
    attempts = 0

    def fail_compute(_db, _now):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("query failed")

    monkeypatch.setattr(team_inbox_sidebar_metrics, "_fallback_snapshot", None)
    monkeypatch.setattr(team_inbox_sidebar_metrics, "_fallback_retry_after", None)
    monkeypatch.setattr(
        team_inbox_sidebar_metrics.app_cache,
        "get_cache_redis",
        lambda: None,
    )
    monkeypatch.setattr(team_inbox_sidebar_metrics, "_compute_snapshot", fail_compute)

    with pytest.raises(RuntimeError, match="query failed"):
        team_inbox_sidebar_metrics.get_sidebar_metrics(object(), now=now)

    snapshot = team_inbox_sidebar_metrics.get_sidebar_metrics(
        object(),
        now=now + timedelta(seconds=1),
    )

    assert attempts == 1
    assert snapshot.queue_metrics.total_open == 0


def test_assignment_totals_are_cached_by_actor_scope(monkeypatch) -> None:
    now = datetime(2026, 11, 14, 9, 30, tzinfo=UTC)
    actor_id = UUID("00000000-0000-0000-0000-000000000101")
    team_id = UUID("00000000-0000-0000-0000-000000000201")
    expected = team_inbox_read.InboxAssignmentCohortCounts(
        all=10,
        queued=8,
        assigned_to_actor=3,
        actor_teams=5,
        ai_handling=2,
    )
    db = MagicMock()
    db.get_bind.return_value.dialect.name = "postgresql"
    calls = 0

    def compute(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return expected

    monkeypatch.setattr(
        team_inbox_sidebar_metrics.app_cache,
        "get_cache_redis",
        lambda: None,
    )
    monkeypatch.setattr(team_inbox_read, "assignment_cohort_counts", compute)

    first = team_inbox_sidebar_metrics.get_assignment_cohort_counts(
        db,
        actor_person_id=actor_id,
        actor_team_ids=(team_id,),
        now=now,
    )
    second = team_inbox_sidebar_metrics.get_assignment_cohort_counts(
        db,
        actor_person_id=actor_id,
        actor_team_ids=(team_id,),
        now=now + timedelta(seconds=10),
    )

    assert first == expected
    assert second == expected
    assert calls == 1


def test_assignment_totals_serve_stale_while_another_worker_refreshes(
    monkeypatch,
) -> None:
    now = datetime(2026, 11, 14, 9, 30, tzinfo=UTC)
    actor_id = UUID("00000000-0000-0000-0000-000000000101")
    expected = team_inbox_read.InboxAssignmentCohortCounts(
        all=10,
        queued=8,
        assigned_to_actor=3,
        actor_teams=5,
        ai_handling=2,
    )
    scope = team_inbox_sidebar_metrics._assignment_scope(actor_id, ())
    stale = team_inbox_sidebar_metrics.InboxAssignmentMetricsSnapshot(
        counts=expected,
        observed_at=now - timedelta(seconds=31),
    )
    db = MagicMock()
    db.get_bind.return_value.dialect.name = "postgresql"
    monkeypatch.setattr(
        team_inbox_sidebar_metrics,
        "_assignment_fallback_snapshots",
        {scope: stale},
    )
    monkeypatch.setattr(
        team_inbox_sidebar_metrics.app_cache,
        "get_json",
        lambda _key: None,
    )
    monkeypatch.setattr(
        team_inbox_sidebar_metrics.app_cache,
        "get_cache_redis",
        lambda: _LeaseClient(claimed=False),
    )
    monkeypatch.setattr(
        team_inbox_read,
        "assignment_cohort_counts",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("unexpected refresh")
        ),
    )

    counts = team_inbox_sidebar_metrics.get_assignment_cohort_counts(
        db,
        actor_person_id=actor_id,
        actor_team_ids=(),
        now=now,
    )

    assert counts == expected
