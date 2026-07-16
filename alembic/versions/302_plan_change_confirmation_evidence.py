"""Bind immediate plan-change confirmation to exact financial evidence.

Revision ID: 302_plan_change_confirmation_evidence
Revises: 301_account_adjustment_evidence
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "302_plan_change_confirmation_evidence"
down_revision = "301_account_adjustment_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subscription_change_requests",
        sa.Column("confirmation_preview_fingerprint", sa.String(64), nullable=True),
    )
    op.add_column(
        "subscription_change_requests",
        sa.Column("confirmation_idempotency_key", sa.String(120), nullable=True),
    )
    op.add_column(
        "subscription_change_requests",
        sa.Column("confirmation_origin", sa.String(40), nullable=True),
    )
    op.add_column(
        "subscription_change_requests",
        sa.Column("confirmation_snapshot", sa.JSON(), nullable=True),
    )
    op.add_column(
        "subscription_change_requests",
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
    )
    for column_name in (
        "account_adjustment_id",
        "credit_note_id",
        "ledger_entry_id",
    ):
        op.add_column(
            "subscription_change_requests",
            sa.Column(column_name, postgresql.UUID(as_uuid=True), nullable=True),
        )

    op.create_unique_constraint(
        "uq_subscription_change_confirmation_idempotency",
        "subscription_change_requests",
        ["confirmation_idempotency_key"],
    )
    op.create_check_constraint(
        "ck_subscription_change_single_financial_owner",
        "subscription_change_requests",
        "account_adjustment_id IS NULL OR credit_note_id IS NULL",
    )
    op.create_foreign_key(
        "fk_subscription_change_account_adjustment",
        "subscription_change_requests",
        "account_adjustments",
        ["account_adjustment_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_subscription_change_credit_note",
        "subscription_change_requests",
        "credit_notes",
        ["credit_note_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_subscription_change_ledger_entry",
        "subscription_change_requests",
        "ledger_entries",
        ["ledger_entry_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "uq_subscription_change_account_adjustment_id",
        "subscription_change_requests",
        ["account_adjustment_id"],
        unique=True,
    )
    op.create_index(
        "uq_subscription_change_credit_note_id",
        "subscription_change_requests",
        ["credit_note_id"],
        unique=True,
    )
    op.create_index(
        "uq_subscription_change_ledger_entry_id",
        "subscription_change_requests",
        ["ledger_entry_id"],
        unique=True,
    )


def downgrade() -> None:
    for index_name in (
        "uq_subscription_change_ledger_entry_id",
        "uq_subscription_change_credit_note_id",
        "uq_subscription_change_account_adjustment_id",
    ):
        op.drop_index(index_name, table_name="subscription_change_requests")
    for constraint_name in (
        "fk_subscription_change_ledger_entry",
        "fk_subscription_change_credit_note",
        "fk_subscription_change_account_adjustment",
    ):
        op.drop_constraint(
            constraint_name,
            "subscription_change_requests",
            type_="foreignkey",
        )
    op.drop_constraint(
        "ck_subscription_change_single_financial_owner",
        "subscription_change_requests",
        type_="check",
    )
    op.drop_constraint(
        "uq_subscription_change_confirmation_idempotency",
        "subscription_change_requests",
        type_="unique",
    )
    for column_name in (
        "ledger_entry_id",
        "credit_note_id",
        "account_adjustment_id",
        "confirmed_at",
        "confirmation_snapshot",
        "confirmation_origin",
        "confirmation_idempotency_key",
        "confirmation_preview_fingerprint",
    ):
        op.drop_column("subscription_change_requests", column_name)
