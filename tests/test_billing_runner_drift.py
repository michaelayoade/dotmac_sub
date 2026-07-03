"""§6.3 runner-heartbeat freshness + §6.6 covered-but-locked drift."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.scheduler import ScheduledTask, ScheduleType
from app.services import billing_health, job_heartbeat
from app.services.billing_health import BillingHealthSnapshot, RunnerHeartbeat

ENF = "app.tasks.collections.run_billing_enforcement"
CYCLE = "app.tasks.billing.run_invoice_cycle"
OVERDUE = "app.tasks.billing.mark_invoices_overdue"


def _task(db, task_name, *, enabled=True, interval=3600):
    db.add(
        ScheduledTask(
            name=task_name.rsplit(".", 1)[-1],
            task_name=task_name,
            schedule_type=ScheduleType.interval,
            interval_seconds=interval,
            enabled=enabled,
        )
    )
    db.commit()


# ---- §6.6 covered-but-locked (SQLite-executable; prod-verified for values) --


def test_covered_but_locked_runs_and_zero_on_empty(db_session):
    # Primary purpose: the raw text() SQL executes under SQLite without error.
    assert billing_health.covered_but_locked(db_session) == 0


# ---- §6.3 runner heartbeats ----------------------------------------------


def test_fresh_runner_not_stale_disabled_not_judged(db_session, monkeypatch):
    _task(db_session, CYCLE, enabled=True, interval=3600)
    _task(db_session, OVERDUE, enabled=False)
    now = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(
        billing_health,
        "get_last_success",
        lambda tn: now - timedelta(minutes=30) if tn == CYCLE else None,
    )
    by = {r.task_name: r for r in billing_health.runner_heartbeats(db_session, now=now)}
    assert by[CYCLE].enabled is True and by[CYCLE].stale is False
    assert by[OVERDUE].enabled is False and by[OVERDUE].stale is False  # disabled
    # an enabled runner missing from the registry resolves to disabled/not-stale
    assert by[ENF].enabled is False and by[ENF].stale is False


def test_stale_when_older_than_interval_times_multiplier(db_session, monkeypatch):
    _task(db_session, ENF, enabled=True, interval=3600)  # stale after 3h
    now = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(
        billing_health,
        "get_last_success",
        lambda tn: now - timedelta(hours=5) if tn == ENF else None,
    )
    by = {r.task_name: r for r in billing_health.runner_heartbeats(db_session, now=now)}
    assert by[ENF].stale is True


def test_enabled_never_succeeded_is_stale(db_session, monkeypatch):
    _task(db_session, ENF, enabled=True, interval=3600)
    monkeypatch.setattr(billing_health, "get_last_success", lambda tn: None)
    by = {r.task_name: r for r in billing_health.runner_heartbeats(db_session)}
    assert by[ENF].enabled is True and by[ENF].stale is True


# ---- anomalies wiring (pure) ---------------------------------------------


def _snap(**kw) -> BillingHealthSnapshot:
    base = dict(
        paid_with_balance_count=0,
        paid_with_balance_total=Decimal("0"),
        last_scanned=100,
        eligible_active_subs=100,
        scan_ratio=1.0,
        payments_24h=10,
        payments_7d_daily_avg=10.0,
        payment_volume_ratio=1.0,
        payment_volume_collapsed=False,
        runners=(),
        covered_but_locked=0,
    )
    base.update(kw)
    return BillingHealthSnapshot(**base)


def test_anomalies_for_new_signals():
    stale_rh = RunnerHeartbeat(ENF, True, 3600, None, None, True)
    assert "runner_heartbeat_stale" in _snap(runners=(stale_rh,)).anomalies
    assert _snap(runners=(stale_rh,)).stale_runners == [ENF]
    assert "enforcement_covered_but_locked" in _snap(covered_but_locked=2).anomalies
    assert _snap().anomalies == []  # healthy default


# ---- last-run result store (round-trip + never-raise) --------------------


class _FakeRedis:
    """Minimal in-memory stand-in for the Redis client used by job_heartbeat."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def set(self, key, value, ex=None):  # noqa: D401 - signature match
        self.store[key] = value

    def get(self, key):
        return self.store.get(key)


def test_record_result_round_trip(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(job_heartbeat, "_get_redis", lambda: fake)
    now = datetime(2026, 7, 3, 9, 0, tzinfo=UTC)
    assert job_heartbeat.record_result(
        ENF, status="ok", detail={"processed": 42, "failed": 0}, now=now
    )
    blob = job_heartbeat.get_last_result(ENF)
    assert blob is not None
    assert blob["status"] == "ok"
    assert blob["at"] == now.isoformat()
    assert blob["detail"] == {"processed": 42, "failed": 0}
    # It writes under its own key prefix, leaving the success heartbeat untouched.
    assert job_heartbeat._RESULT_KEY_PREFIX + ENF in fake.store
    assert job_heartbeat._KEY_PREFIX + ENF not in fake.store


def test_record_result_error_status(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(job_heartbeat, "_get_redis", lambda: fake)
    assert job_heartbeat.record_result(ENF, status="error", detail={"error": "boom"})
    blob = job_heartbeat.get_last_result(ENF)
    assert blob["status"] == "error" and blob["detail"] == {"error": "boom"}


def test_get_last_result_missing_key_returns_none(monkeypatch):
    monkeypatch.setattr(job_heartbeat, "_get_redis", lambda: _FakeRedis())
    assert job_heartbeat.get_last_result(ENF) is None


def test_result_store_never_raises_when_redis_down(monkeypatch):
    monkeypatch.setattr(job_heartbeat, "_get_redis", lambda: None)
    assert job_heartbeat.record_result(ENF, status="ok", detail={"x": 1}) is False
    assert job_heartbeat.get_last_result(ENF) is None


# ---- last-run result surfaced in the view-model --------------------------


def test_runner_heartbeats_include_last_result(db_session, monkeypatch):
    _task(db_session, ENF, enabled=True, interval=3600)
    now = datetime(2026, 7, 3, 9, 0, tzinfo=UTC)
    monkeypatch.setattr(billing_health, "get_last_success", lambda tn: now)
    result_blob = {
        "status": "ok",
        "at": now.isoformat(),
        "detail": {"processed": 42, "failed": 0},
    }
    monkeypatch.setattr(
        billing_health,
        "get_last_result",
        lambda tn: result_blob if tn == ENF else None,
    )
    by = {r.task_name: r for r in billing_health.runner_heartbeats(db_session, now=now)}
    rh = by[ENF]
    assert rh.last_result == result_blob
    assert rh.last_result_status == "ok"
    assert rh.last_result_at == now
    assert "processed=42" in rh.last_result_summary


def test_runner_heartbeat_last_result_helpers_defaults():
    rh = RunnerHeartbeat(ENF, True, 3600, None, None, True)
    assert rh.last_result is None
    assert rh.last_result_status is None
    assert rh.last_result_at is None
    assert rh.last_result_detail is None
    assert rh.last_result_summary == "No result yet"
    err = RunnerHeartbeat(
        ENF, True, 3600, None, None, True, {"status": "error", "detail": {"error": "x"}}
    )
    assert err.last_result_summary == "errored: x"
