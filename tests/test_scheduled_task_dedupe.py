"""_sync_scheduled_task must keep exactly one row per task name.

Regression: the sync matched by task_name, so a task rename/move inserted a new
row and orphaned the old one under the same name (duplicate rows). It now
matches by name and updates the task_name in place.
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import create_engine

from app.models.scheduler import ScheduledTask
from app.services import scheduler_config

# Migration 172's dedupe statement (kept in sync with upgrade()).
DEDUPE_SQL = """
    DELETE FROM scheduled_tasks
    WHERE id IN (
        SELECT id FROM (
            SELECT id, row_number() OVER (
                PARTITION BY name
                ORDER BY enabled DESC, updated_at DESC, id
            ) AS rn
            FROM scheduled_tasks
        ) ranked
        WHERE rn > 1
    )
"""


def _count(db, name: str) -> int:
    return db.query(ScheduledTask).filter(ScheduledTask.name == name).count()


def test_migration_dedupe_collapses_duplicate_names():
    """Migration 172 collapses pre-existing duplicate-name rows to exactly one.

    The unique constraint can't coexist with duplicate rows, so seed a
    constraint-free table (simulating pre-migration data), run the migration's
    dedupe SQL, then add the constraint and assert one row survives per name
    (enabled-then-newest wins).
    """
    engine = create_engine("sqlite://")
    meta = sa.MetaData()
    # A scheduled_tasks table WITHOUT uq_scheduled_tasks_name, mirroring the
    # state before migration 172 ran.
    tasks = sa.Table(
        "scheduled_tasks",
        meta,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("name", sa.String, nullable=False),
        sa.Column("task_name", sa.String, nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
    )
    meta.create_all(engine)

    rows = [
        # name="dup": three rows; the enabled, newest one must survive.
        {
            "id": "dup-old",
            "name": "dup",
            "task_name": "a.old",
            "enabled": False,
            "updated_at": datetime(2026, 1, 1),
        },
        {
            "id": "dup-mid",
            "name": "dup",
            "task_name": "a.mid",
            "enabled": True,
            "updated_at": datetime(2026, 2, 1),
        },
        {
            "id": "keep-dup",
            "name": "dup",
            "task_name": "a.new",
            "enabled": True,
            "updated_at": datetime(2026, 3, 1),
        },
        # name="solo": a lone row must be left untouched.
        {
            "id": "keep-solo",
            "name": "solo",
            "task_name": "b.run",
            "enabled": True,
            "updated_at": datetime(2026, 1, 1),
        },
    ]
    with engine.begin() as conn:
        conn.execute(tasks.insert(), rows)
        conn.execute(sa.text(DEDUPE_SQL))
        # Constraint now applies cleanly.
        conn.execute(
            sa.text(
                "CREATE UNIQUE INDEX uq_scheduled_tasks_name ON scheduled_tasks(name)"
            )
        )
        survivors = conn.execute(
            sa.select(tasks.c.id, tasks.c.name).order_by(tasks.c.name)
        ).all()

    assert [(r.id, r.name) for r in survivors] == [
        ("keep-dup", "dup"),
        ("keep-solo", "solo"),
    ]


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
