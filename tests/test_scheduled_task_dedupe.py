"""_sync_scheduled_task must keep exactly one row per task name.

Regression: the sync matched by task_name, so a task rename/move inserted a new
row and orphaned the old one under the same name (duplicate rows). It now
matches by name and updates the task_name in place.
"""

from __future__ import annotations

from app.models.scheduler import ScheduledTask
from app.services import scheduler_config


def _count(db, name: str) -> int:
    return db.query(ScheduledTask).filter(ScheduledTask.name == name).count()


def test_sync_is_idempotent(db_session):
    for _ in range(3):
        scheduler_config._sync_scheduled_task(
            db_session,
            name="dedupe_idem",
            task_name="app.tasks.demo.a",
            enabled=True,
            interval_seconds=3600,
        )
    assert _count(db_session, "dedupe_idem") == 1


def test_sync_task_rename_updates_in_place(db_session):
    # Original task_name...
    scheduler_config._sync_scheduled_task(
        db_session,
        name="dedupe_rename",
        task_name="app.tasks.demo.old",
        enabled=True,
        interval_seconds=3600,
    )
    # ...then the task is renamed/moved. The old bug inserted a second row;
    # now it must update the single row in place (no duplicate).
    scheduler_config._sync_scheduled_task(
        db_session,
        name="dedupe_rename",
        task_name="app.tasks.demo.new",
        enabled=True,
        interval_seconds=3600,
    )
    rows = (
        db_session.query(ScheduledTask)
        .filter(ScheduledTask.name == "dedupe_rename")
        .all()
    )
    assert len(rows) == 1
    assert rows[0].task_name == "app.tasks.demo.new"
