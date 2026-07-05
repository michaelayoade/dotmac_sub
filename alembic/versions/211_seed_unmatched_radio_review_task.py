"""Seed the hourly unmatched-radio review beat task.

Registers ``topology_unmatched_radio_review`` as a ``scheduled_tasks`` row
instead of a hardcoded ``build_beat_schedule`` entry: the builder's generic
enabled-rows loop schedules every enabled row, so no scheduler_config change
is needed and the interval stays editable from the admin scheduler UI.

Revision ID: 211_seed_unmatched_radio_review_task
Revises: 210_crm_payment_idempotency
"""

from __future__ import annotations

from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision = "211_seed_unmatched_radio_review_task"
down_revision = "210_crm_payment_idempotency"
branch_labels = None
depends_on = None

TASK_NAME = "topology_unmatched_radio_review"
TASK_PATH = "app.tasks.unmatched_radio.run_unmatched_radio_review"


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            INSERT INTO scheduled_tasks
                (id, name, task_name, schedule_type, interval_seconds,
                 enabled, created_at, updated_at)
            SELECT :id, :name, :task_path, 'interval', 3600,
                   true, now(), now()
            WHERE NOT EXISTS (
                SELECT 1 FROM scheduled_tasks WHERE name = :name
            )
            """
        ).bindparams(id=str(uuid4()), name=TASK_NAME, task_path=TASK_PATH)
    )


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM scheduled_tasks WHERE name = :name").bindparams(
            name=TASK_NAME
        )
    )
