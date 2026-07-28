"""Add reviewed service-change reconciliation evidence.

Revision ID: 403_service_change_reconciliation_evidence
Revises: 402_remote_reprovision_verification
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "403_service_change_reconciliation_evidence"
down_revision = "402_remote_reprovision_verification"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for column in (
        "reconciliation_idempotency_key_hash",
        "reconciliation_reviewed_head",
    ):
        op.add_column(
            "subscription_change_requests",
            sa.Column(column, sa.String(length=64), nullable=True),
        )
    op.add_column(
        "subscription_change_requests",
        sa.Column("reconciliation_actor_id", sa.String(length=120), nullable=True),
    )
    op.add_column(
        "subscription_change_requests",
        sa.Column("reconciliation_reason", sa.Text(), nullable=True),
    )
    op.add_column(
        "subscription_change_requests",
        sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_sub_change_reconciliation_key_hash",
        "subscription_change_requests",
        ["reconciliation_idempotency_key_hash"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_sub_change_reconciliation_key_hash",
        table_name="subscription_change_requests",
    )
    for column in (
        "reconciled_at",
        "reconciliation_reason",
        "reconciliation_actor_id",
        "reconciliation_reviewed_head",
        "reconciliation_idempotency_key_hash",
    ):
        op.drop_column("subscription_change_requests", column)
