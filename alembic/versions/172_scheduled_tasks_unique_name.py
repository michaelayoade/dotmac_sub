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

from alembic import op

revision = "172_scheduled_tasks_unique_name"
down_revision = "171_system_user_device_login"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Self-healing: collapse any duplicate names before adding the constraint.
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
    op.create_unique_constraint(
        "uq_scheduled_tasks_name", "scheduled_tasks", ["name"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_scheduled_tasks_name", "scheduled_tasks", type_="unique")
