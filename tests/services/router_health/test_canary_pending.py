"""Pending canary re-probes: per-NAS locks and escalation of lost re-probes."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.services.router_health import service_path
from app.services.router_health.contracts import CheckId, HealthPriority
from app.services.router_health.settings import RouterHealthSettings
from app.tasks import router_health as tasks

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


def nas_row(*, disposition, age_minutes, eligible=True):
    return service_path.NasInput(
        nas_device_id=uuid4(),
        name="nas",
        router_id=None,
        router_name=None,
        canary_eligible=eligible,
        last_canary_at=NOW - timedelta(minutes=age_minutes),
        last_canary_disposition=disposition,
        coa_counts={},
        enforcement_failures=[],
    )


def compute(row, *, enabled=True, **kw):
    return service_path.compute_service_path(
        nas_rows=[row],
        settings=RouterHealthSettings(**kw),
        canary_enabled=enabled,
        now=NOW,
    )


def checks(result):
    return {f.check: f.priority for f in result.findings}


def test_recent_pending_failure_stays_pending() -> None:
    result = compute(nas_row(disposition="timeout", age_minutes=10))
    assert CheckId.coa_canary_unverified not in checks(result)


def test_lost_reprobe_escalates_to_unverified_high() -> None:
    # default re-probe delay is 300 s; the limit is max(3 x 300 s, 15 min).
    for disposition in ("timeout", "transport_error"):
        result = compute(nas_row(disposition=disposition, age_minutes=16))
        assert checks(result)[CheckId.coa_canary_unverified] is HealthPriority.high


def test_healthy_or_disabled_or_ineligible_does_not_escalate() -> None:
    assert CheckId.coa_canary_unverified not in checks(
        compute(nas_row(disposition="rejected", age_minutes=600))
    )
    assert CheckId.coa_canary_unverified not in checks(
        compute(nas_row(disposition="timeout", age_minutes=600), enabled=False)
    )
    assert CheckId.coa_canary_unverified not in checks(
        compute(nas_row(disposition="timeout", age_minutes=600, eligible=False))
    )


def test_unverified_scope_is_owned_so_a_later_healthy_probe_resolves_it() -> None:
    result = compute(nas_row(disposition="rejected", age_minutes=5))
    assert any(s.check is CheckId.coa_canary_unverified for s in result.scopes)


def test_reprobe_lock_is_per_nas(monkeypatch) -> None:
    keys: list[int] = []

    @contextmanager
    def fake_lock(key, timeout_ms):
        keys.append(key)
        yield True

    class FakeDb:
        def close(self) -> None:
            return None

    monkeypatch.setattr(tasks, "postgres_session_advisory_lock", fake_lock)
    monkeypatch.setattr(tasks.control_registry, "is_enabled", lambda db, k: True)
    monkeypatch.setattr(tasks.db_session_adapter, "create_session", lambda: FakeDb())
    monkeypatch.setattr(tasks.observability, "record_task_run", lambda *a, **k: None)
    for nas_id in ("a", "b", "a"):
        tasks._run(tasks.TASK_REPROBE, "c", lambda db: {}, lock_scope=nas_id)
    assert keys[0] != keys[1] and keys[0] == keys[2]
    tasks._run(tasks.TASK_REPROBE, "c", lambda db: {})
    assert keys[3] not in {keys[0], keys[1]}
