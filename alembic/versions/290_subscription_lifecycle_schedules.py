"""Add durable deferred subscription lifecycle schedules.

Revision ID: 290_subscription_lifecycle_schedules
Revises: 289_merge_support_subscription_and_firmware_heads
Create Date: 2026-07-14
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "290_subscription_lifecycle_schedules"
down_revision = "289_merge_support_subscription_and_firmware_heads"
branch_labels = None
depends_on = None

_TABLE = "subscription_lifecycle_schedules"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "subscription_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("command_kind", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=120), nullable=False),
        sa.Column("effective_timing", sa.String(length=32), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("reviewed_head", sa.String(length=64), nullable=False),
        sa.Column("command_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=True),
        sa.Column("actor_id", sa.String(length=120), nullable=True),
        sa.Column(
            "actor_type",
            sa.String(length=32),
            nullable=False,
            server_default="system",
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_by", sa.String(length=120), nullable=True),
        sa.Column("last_error_code", sa.String(length=120), nullable=True),
        sa.Column("last_message", sa.Text(), nullable=True),
        sa.Column("outcome_head", sa.String(length=64), nullable=True),
        sa.Column("artifact_ids", sa.JSON(), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("canceled_by", sa.String(length=120), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"], ["subscriptions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "subscription_id",
            "idempotency_key",
            name="uq_subscription_lifecycle_schedule_idempotency",
        ),
    )
    op.create_index(
        "ix_subscription_lifecycle_schedule_due",
        _TABLE,
        ["status", "next_attempt_at", "effective_at"],
    )
    op.create_index(
        "ix_subscription_lifecycle_schedule_subscription",
        _TABLE,
        ["subscription_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_subscription_lifecycle_schedule_subscription", table_name=_TABLE)
    op.drop_index("ix_subscription_lifecycle_schedule_due", table_name=_TABLE)
    op.drop_table(_TABLE)
