"""add provisioning_step_executions table

Revision ID: 047_add_provisioning_step_executions
Revises: 046_add_restore_olt_from_backup_step_type
Create Date: 2026-04-21
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "047_add_provisioning_step_executions"
down_revision = "046_add_restore_olt_from_backup_step_type"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    provisioningstepexecutionstatus = postgresql.ENUM(
        "pending",
        "running",
        "succeeded",
        "failed",
        "skipped",
        "compensated",
        name="provisioningstepexecutionstatus",
        create_type=False,
    )
    provisioningstepexecutionstatus.create(bind, checkfirst=True)

    if "provisioning_step_executions" in inspector.get_table_names():
        return

    op.create_table(
        "provisioning_step_executions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("saga_execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("saga_name", sa.String(length=128), nullable=False),
        sa.Column("correlation_key", sa.String(length=256), nullable=True),
        sa.Column("step_name", sa.String(length=128), nullable=False),
        sa.Column("step_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", provisioningstepexecutionstatus, nullable=False),
        sa.Column(
            "result_data",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["saga_execution_id"],
            ["saga_executions.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "saga_execution_id",
            "step_name",
            name="uq_provisioning_step_execution_per_attempt",
        ),
    )

    op.create_index(
        "ix_provisioning_step_executions_correlation_step",
        "provisioning_step_executions",
        ["correlation_key", "saga_name", "step_name"],
    )
    op.create_index(
        "ix_provisioning_step_executions_status",
        "provisioning_step_executions",
        ["status"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if "provisioning_step_executions" in inspector.get_table_names():
        op.drop_index(
            "ix_provisioning_step_executions_status",
            table_name="provisioning_step_executions",
        )
        op.drop_index(
            "ix_provisioning_step_executions_correlation_step",
            table_name="provisioning_step_executions",
        )
        op.drop_table("provisioning_step_executions")

    provisioningstepexecutionstatus = sa.Enum(name="provisioningstepexecutionstatus")
    provisioningstepexecutionstatus.drop(bind, checkfirst=True)
