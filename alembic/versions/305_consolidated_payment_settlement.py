"""Add exact consolidated payment settlement ledger evidence.

Revision ID: 305_consolidated_payment_settlement
Revises: 304_grace_walled_garden_policy
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "305_consolidated_payment_settlement"
down_revision = "304_grace_walled_garden_policy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ledger_entry_type = postgresql.ENUM(
        "debit", "credit", name="ledgerentrytype", create_type=False
    )
    ledger_source = postgresql.ENUM(
        "invoice",
        "payment",
        "adjustment",
        "refund",
        "credit_note",
        "other",
        name="ledgersource",
        create_type=False,
    )
    op.create_table(
        "billing_account_ledger_entries",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True
        ),
        sa.Column("billing_account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("entry_type", ledger_entry_type, nullable=False),
        sa.Column("source", ledger_source, nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("balance_after", sa.Numeric(12, 2), nullable=False),
        sa.Column("memo", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "amount > 0", name="ck_billing_account_ledger_entries_amount_positive"
        ),
        sa.ForeignKeyConstraint(
            ["billing_account_id"],
            ["billing_accounts.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["payment_id"], ["payments.id"], ondelete="RESTRICT"),
    )
    op.create_index(
        "uq_billing_account_ledger_entries_payment_credit",
        "billing_account_ledger_entries",
        ["payment_id"],
        unique=True,
        postgresql_where=sa.text(
            "payment_id IS NOT NULL AND entry_type = 'credit' AND is_active"
        ),
    )
    op.add_column(
        "payment_settlements",
        sa.Column(
            "billing_account_ledger_entry_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_payment_settlements_billing_account_ledger_entry_id",
        "payment_settlements",
        "billing_account_ledger_entries",
        ["billing_account_ledger_entry_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "uq_payment_settlements_billing_account_ledger_entry_id",
        "payment_settlements",
        ["billing_account_ledger_entry_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_payment_settlements_billing_account_ledger_entry_id",
        table_name="payment_settlements",
    )
    op.drop_constraint(
        "fk_payment_settlements_billing_account_ledger_entry_id",
        "payment_settlements",
        type_="foreignkey",
    )
    op.drop_column("payment_settlements", "billing_account_ledger_entry_id")
    op.drop_index(
        "uq_billing_account_ledger_entries_payment_credit",
        table_name="billing_account_ledger_entries",
    )
    op.drop_table("billing_account_ledger_entries")
