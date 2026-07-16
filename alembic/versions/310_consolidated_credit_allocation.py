"""Add exact consolidated-credit allocation evidence.

Revision ID: 310_consolidated_credit_allocation
Revises: 309_retire_feature_aliases
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "310_consolidated_credit_allocation"
down_revision = "309_retire_feature_aliases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "billing_account_credit_allocations",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True
        ),
        sa.Column("billing_account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subscriber_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "billing_account_ledger_entry_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("preview_fingerprint", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "amount > 0", name="ck_billing_account_credit_allocations_amount_positive"
        ),
        sa.ForeignKeyConstraint(
            ["billing_account_id"],
            ["billing_accounts.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["subscriber_id"], ["subscribers.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["billing_account_ledger_entry_id"],
            ["billing_account_ledger_entries.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "billing_account_ledger_entry_id",
            name="uq_billing_account_credit_allocations_debit_entry",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_billing_account_credit_allocations_idempotency_key",
        ),
    )
    op.create_index(
        "ix_billing_account_credit_allocations_account_created",
        "billing_account_credit_allocations",
        ["billing_account_id", "created_at"],
    )
    op.create_table(
        "billing_account_credit_allocation_items",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True
        ),
        sa.Column("allocation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "source_billing_account_ledger_entry_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "payment_allocation_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "subscriber_ledger_entry_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "amount > 0",
            name="ck_billing_account_credit_allocation_items_amount_positive",
        ),
        sa.ForeignKeyConstraint(
            ["allocation_id"],
            ["billing_account_credit_allocations.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_billing_account_ledger_entry_id"],
            ["billing_account_ledger_entries.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["payment_allocation_id"],
            ["payment_allocations.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["subscriber_ledger_entry_id"],
            ["ledger_entries.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "payment_allocation_id",
            name="uq_billing_account_credit_allocation_items_payment_allocation",
        ),
        sa.UniqueConstraint(
            "subscriber_ledger_entry_id",
            name="uq_billing_account_credit_allocation_items_subscriber_ledger",
        ),
    )
    op.create_index(
        "ix_billing_account_credit_allocation_items_allocation",
        "billing_account_credit_allocation_items",
        ["allocation_id"],
    )
    op.create_index(
        "ix_billing_account_credit_allocation_items_source",
        "billing_account_credit_allocation_items",
        ["source_billing_account_ledger_entry_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_billing_account_credit_allocation_items_source",
        table_name="billing_account_credit_allocation_items",
    )
    op.drop_index(
        "ix_billing_account_credit_allocation_items_allocation",
        table_name="billing_account_credit_allocation_items",
    )
    op.drop_table("billing_account_credit_allocation_items")
    op.drop_index(
        "ix_billing_account_credit_allocations_account_created",
        table_name="billing_account_credit_allocations",
    )
    op.drop_table("billing_account_credit_allocations")
