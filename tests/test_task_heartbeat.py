"""Shared task heartbeat: record, skip streaks, snapshots."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.services import task_heartbeat


class _FakeCache:
    def __init__(self):
        self.store: dict[str, object] = {}

    def get_json(self, key):
        return self.store.get(key)

    def set_json(self, key, value, ttl_seconds):
        self.store[key] = value
        return True


def _wire(monkeypatch) -> _FakeCache:
    cache = _FakeCache()
    monkeypatch.setattr("app.services.app_cache.get_json", cache.get_json)
    monkeypatch.setattr("app.services.app_cache.set_json", cache.set_json)
    return cache


def test_success_keeps_numbers_only_and_resets_streak(monkeypatch):
    _wire(monkeypatch)
    task_heartbeat.record_skip("t1")
    task_heartbeat.record_skip("t1")

    task_heartbeat.record_success(
        "t1", {"count": 5, "freshness": 12.5, "flag": True, "note": "x"}
    )
    snap = task_heartbeat.snapshot("t1")

    assert snap["result"] == {"count": 5, "freshness": 12.5}
    assert snap["skip_streak"] == 0
    assert snap["last_success_age_seconds"] is not None
    assert snap["last_success_age_seconds"] < 5


def test_snapshot_age_and_isolation_between_tasks(monkeypatch):
    _wire(monkeypatch)
    now = datetime.now(UTC)
    task_heartbeat.record_success("t1", {"a": 1}, now=now - timedelta(seconds=300))
    task_heartbeat.record_skip("t2")

    snap1 = task_heartbeat.snapshot("t1", now=now)
    snap2 = task_heartbeat.snapshot("t2", now=now)

    assert 295 <= snap1["last_success_age_seconds"] <= 305
    assert snap1["skip_streak"] == 0
    assert snap2["last_success_age_seconds"] is None
    assert snap2["skip_streak"] == 1


def test_never_recorded_snapshot(monkeypatch):
    _wire(monkeypatch)
    snap = task_heartbeat.snapshot("ghost")
    assert snap == {
        "last_success_age_seconds": None,
        "skip_streak": 0,
        "result": {},
    }
