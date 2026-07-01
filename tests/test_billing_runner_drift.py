"""§6.3 runner-heartbeat freshness + §6.6 covered-but-locked drift."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.scheduler import ScheduledTask, ScheduleType
from app.services import billing_health
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
