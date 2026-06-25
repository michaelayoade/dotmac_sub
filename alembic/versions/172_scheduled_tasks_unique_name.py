"""Dedupe scheduled_tasks and enforce unique(name).

Duplicate rows accumulated because the scheduler synced by ``task_name``: a task
rename/move (e.g. run_dunning -> run_billing_enforcement) inserted a new row and
orphaned the old one under the same ``name``. The sync now matches by ``name``;
this migration removes any existing duplicates (keeping the enabled, then newest
row per name) and adds a unique constraint so it cannot recur.

Revision ID: 172_scheduled_tasks_unique_name
Revises: 171_system_user_device_login
Create Date: 2026-06-24
"""

from __future__ import annotations

from sqlalchemy import inspect

from alembic import op

revision = "172_scheduled_tasks_unique_name"
down_revision = "171_system_user_device_login"
branch_labels = None
depends_on = None

CONSTRAINT_NAME = "uq_scheduled_tasks_name"
TABLE_NAME = "scheduled_tasks"


def _has_constraint() -> bool:
    inspector = inspect(op.get_bind())
    return any(
        c["name"] == CONSTRAINT_NAME
        for c in inspector.get_unique_constraints(TABLE_NAME)
    )


def upgrade() -> None:
    # Self-healing: collapse any duplicate names before adding the constraint.
    # Intentional history drop: surplus duplicate rows are hard-DELETEd (no FK
    # references scheduled_tasks.id, so nothing depends on the discarded ids).
    # We keep the enabled, then most-recently-updated row per name.
    op.execute(
        """
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
    )
    # Idempotent: only create the constraint if it isn't already present.
    if not _has_constraint():
        op.create_unique_constraint(CONSTRAINT_NAME, TABLE_NAME, ["name"])


def downgrade() -> None:
    if _has_constraint():
        op.drop_constraint(CONSTRAINT_NAME, TABLE_NAME, type_="unique")
