"""Add durable network-operation dispatch outbox.

Revision ID: 294_network_operation_dispatch_outbox
Revises: 293_network_operation_redrive
Create Date: 2026-07-14
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "294_network_operation_dispatch_outbox"
down_revision = "293_network_operation_redrive"
branch_labels = None
depends_on = None

_STATUS_NAME = "networkoperationdispatchstatus"
_STATUS_VALUES = (
    "pending",
    "dispatched",
    "acknowledged",
    "completed",
    "failed",
    "reconciliation_needed",
    "canceled",
)


def upgrade() -> None:
    status_enum = postgresql.ENUM(*_STATUS_VALUES, name=_STATUS_NAME)
    status_enum.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "network_operation_dispatches",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dispatch_key", sa.String(length=80), nullable=False),
        sa.Column("command_name", sa.String(length=120), nullable=False),
        sa.Column("task_name", sa.String(length=180), nullable=False),
        sa.Column("args_payload", sa.JSON(), nullable=False),
        sa.Column("kwargs_payload", sa.JSON(), nullable=False),
        sa.Column("queue", sa.String(length=80), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(name=_STATUS_NAME, create_type=False),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("task_id", sa.String(length=120), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "attempts >= 0 AND max_attempts > 0",
            name="ck_netop_dispatch_attempt_budget",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["network_operations.id"],
            name="fk_netop_dispatch_operation_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_netop_dispatch_operation_key",
        "network_operation_dispatches",
        ["operation_id", "dispatch_key"],
        unique=True,
    )
    op.create_index(
        "ix_netop_dispatch_ready",
        "network_operation_dispatches",
        ["status", "next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_netop_dispatch_ready", table_name="network_operation_dispatches")
    op.drop_index(
        "uq_netop_dispatch_operation_key",
        table_name="network_operation_dispatches",
    )
    op.drop_table("network_operation_dispatches")
    postgresql.ENUM(name=_STATUS_NAME).drop(op.get_bind(), checkfirst=True)
