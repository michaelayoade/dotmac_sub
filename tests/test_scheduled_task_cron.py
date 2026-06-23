"""Tests for crontab scheduling of ScheduledTask rows (phase 3)."""

import uuid

from celery.schedules import crontab

from app.models.scheduler import ScheduledTask, ScheduleType
from app.services import scheduler_config


def test_cron_parser_valid_expression():
    sched = scheduler_config._cron_to_beat_schedule("30 9 * * 1-5")
    assert isinstance(sched, crontab)
    assert sched.minute == {30}
    assert sched.hour == {9}
    assert sched.day_of_week == {1, 2, 3, 4, 5}  # Mon-Fri


def test_cron_parser_rejects_bad_input():
    assert scheduler_config._cron_to_beat_schedule(None) is None
    assert scheduler_config._cron_to_beat_schedule("") is None
    assert scheduler_config._cron_to_beat_schedule("0 9 * *") is None  # 4 fields
    assert scheduler_config._cron_to_beat_schedule("0 9 * * * *") is None  # 6 fields
    assert scheduler_config._cron_to_beat_schedule("0 99 * * *") is None  # bad hour
    assert scheduler_config._cron_to_beat_schedule("nonsense") is None


def _row(**kw):
    defaults = dict(
        id=uuid.uuid4(),
        name="demo",
        task_name="app.tasks.billing.run_billing_notifications",
        enabled=True,
    )
    defaults.update(kw)
    return ScheduledTask(**defaults)


def test_row_to_entry_crontab():
    task = _row(schedule_type=ScheduleType.crontab, cron_expr="0 9 * * *")
    entry = scheduler_config._scheduled_row_to_entry(task)
    assert entry is not None
    key, body = entry
    assert key == f"scheduled_task_{task.id}"
    assert body["task"] == task.task_name
    assert isinstance(body["schedule"], crontab)
    assert body["schedule"].hour == {9}
    assert body["schedule"].minute == {0}
    # crontab entries don't carry an interval-based expiry
    assert "expires" not in body["options"]


def test_row_to_entry_crontab_invalid_is_skipped():
    task = _row(schedule_type=ScheduleType.crontab, cron_expr="not a cron")
    assert scheduler_config._scheduled_row_to_entry(task) is None


def test_row_to_entry_interval_still_works():
    task = _row(schedule_type=ScheduleType.interval, interval_seconds=3600)
    entry = scheduler_config._scheduled_row_to_entry(task)
    assert entry is not None
    _, body = entry
    # interval schedule is a timedelta or anchored crontab, never None
    assert body["schedule"] is not None
    assert "expires" in body["options"]
