"""Tests for the scheduler cron admin UI route + helpers (phase 4)."""

import uuid
from datetime import UTC, datetime

from app.models.scheduler import ScheduledTask, ScheduleType
from app.services import scheduler_config
from app.web.admin import system


def test_is_valid_cron():
    assert scheduler_config.is_valid_cron("0 9 * * *")
    assert scheduler_config.is_valid_cron("30 8 * * 1-5")
    assert not scheduler_config.is_valid_cron("bad")
    assert not scheduler_config.is_valid_cron("0 9 * *")
    assert not scheduler_config.is_valid_cron(None)


def test_next_cron_run_is_in_future():
    now = datetime.now(UTC)
    nxt = scheduler_config.next_cron_run("* * * * *")  # every minute
    assert nxt is not None and nxt > now
    assert scheduler_config.next_cron_run("bad") is None


def _task(db, **kw):
    defaults = dict(
        id=uuid.uuid4(),
        name="demo",
        task_name="app.tasks.billing.run_billing_notifications",
        schedule_type=ScheduleType.interval,
        interval_seconds=3600,
        enabled=True,
    )
    defaults.update(kw)
    task = ScheduledTask(**defaults)
    db.add(task)
    db.commit()
    return task


def test_route_switch_to_cron(db_session):
    task = _task(db_session)
    resp = system.scheduler_task_update_schedule(
        None,
        str(task.id),
        schedule_type="crontab",
        interval_seconds="",
        cron_expr="0 9 * * *",
        db=db_session,
    )
    assert resp.status_code == 303
    assert "saved=1" in resp.headers["location"]
    db_session.refresh(task)
    assert task.schedule_type == ScheduleType.crontab
    assert task.cron_expr == "0 9 * * *"


def test_route_invalid_cron_rejected(db_session):
    task = _task(db_session)
    resp = system.scheduler_task_update_schedule(
        None,
        str(task.id),
        schedule_type="crontab",
        interval_seconds="",
        cron_expr="nope",
        db=db_session,
    )
    assert "error=invalid_cron" in resp.headers["location"]
    db_session.refresh(task)
    # unchanged
    assert task.schedule_type == ScheduleType.interval
    assert task.cron_expr is None


def test_route_switch_back_to_interval_clears_cron(db_session):
    task = _task(db_session, schedule_type=ScheduleType.crontab, cron_expr="0 9 * * *")
    resp = system.scheduler_task_update_schedule(
        None,
        str(task.id),
        schedule_type="interval",
        interval_seconds="7200",
        cron_expr="",
        db=db_session,
    )
    assert "saved=1" in resp.headers["location"]
    db_session.refresh(task)
    assert task.schedule_type == ScheduleType.interval
    assert task.interval_seconds == 7200
    assert task.cron_expr is None


def test_route_invalid_interval_rejected(db_session):
    task = _task(db_session)
    resp = system.scheduler_task_update_schedule(
        None,
        str(task.id),
        schedule_type="interval",
        interval_seconds="0",
        cron_expr="",
        db=db_session,
    )
    assert "error=invalid_interval" in resp.headers["location"]
