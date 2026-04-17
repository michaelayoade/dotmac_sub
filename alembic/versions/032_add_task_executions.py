"""Add task_executions table for task idempotency

Revision ID: 032_add_task_executions
Revises: 031_add_signal_threshold_overrides
Create Date: 2026-04-17
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "032_add_task_executions"
down_revision = "031_add_signal_threshold_overrides"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    # Check if table already exists (idempotent)
    if "task_executions" in inspector.get_table_names():
        return

    # Create the task_execution_status enum
    task_execution_status = sa.Enum(
        "running", "succeeded", "failed", name="taskexecutionstatus"
    )
    task_execution_status.create(bind, checkfirst=True)

    op.create_table(
        "task_executions",
        sa.Column(
            "id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True
        ),
        sa.Column(
            "idempotency_key", sa.String(255), unique=True, nullable=False
        ),
        sa.Column("task_name", sa.String(255), nullable=False, index=True),
        sa.Column(
            "status",
            task_execution_status,
            nullable=False,
            index=True,
        ),
        sa.Column("celery_task_id", sa.String(255), nullable=True),
        sa.Column(
            "result", sa.dialects.postgresql.JSONB, nullable=True
        ),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Create composite index for finding stale running tasks
    op.create_index(
        "ix_task_executions_status_created",
        "task_executions",
        ["status", "created_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if "task_executions" in inspector.get_table_names():
        op.drop_index("ix_task_executions_status_created", table_name="task_executions")
        op.drop_table("task_executions")

    # Drop the enum type
    task_execution_status = sa.Enum(name="taskexecutionstatus")
    task_execution_status.drop(bind, checkfirst=True)
