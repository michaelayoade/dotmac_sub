"""Add exact consolidated payment refund and reversal evidence.

Revision ID: 315_consolidated_payment_returns
Revises: 314_consolidated_credit_allocation
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "315_consolidated_payment_returns"
down_revision = "314_consolidated_credit_allocation"
branch_labels = None
depends_on = None


def _add_billing_account_result(table: str) -> None:
    op.alter_column(
        table, "ledger_entry_id", existing_type=postgresql.UUID(), nullable=True
    )
    op.add_column(
        table,
        sa.Column(
            "billing_account_ledger_entry_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        f"fk_{table}_billing_account_ledger_entry_id",
        table,
        "billing_account_ledger_entries",
        ["billing_account_ledger_entry_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        f"uq_{table}_billing_account_ledger_entry_id",
        table,
        ["billing_account_ledger_entry_id"],
        unique=True,
    )


def upgrade() -> None:
    _add_billing_account_result("payment_refunds")
    _add_billing_account_result("payment_reversals")
    op.create_table(
        "consolidated_payment_return_allocation_evidence",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True
        ),
        sa.Column("refund_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reversal_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "payment_allocation_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("ledger_entry_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(CASE WHEN refund_id IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN reversal_id IS NOT NULL THEN 1 ELSE 0 END) = 1",
            name="ck_consolidated_return_evidence_exactly_one_owner",
        ),
        sa.ForeignKeyConstraint(
            ["refund_id"], ["payment_refunds.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["reversal_id"], ["payment_reversals.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["payment_allocation_id"],
            ["payment_allocations.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["ledger_entry_id"], ["ledger_entries.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "refund_id",
            "payment_allocation_id",
            name="uq_consolidated_refund_allocation_evidence",
        ),
        sa.UniqueConstraint(
            "reversal_id",
            "payment_allocation_id",
            name="uq_consolidated_reversal_allocation_evidence",
        ),
    )
    op.create_index(
        "uq_consolidated_return_allocation_ledger_entry_id",
        "consolidated_payment_return_allocation_evidence",
        ["ledger_entry_id"],
        unique=True,
    )


def _drop_billing_account_result(table: str) -> None:
    op.drop_index(f"uq_{table}_billing_account_ledger_entry_id", table_name=table)
    op.drop_constraint(
        f"fk_{table}_billing_account_ledger_entry_id", table, type_="foreignkey"
    )
    op.drop_column(table, "billing_account_ledger_entry_id")
    op.alter_column(
        table, "ledger_entry_id", existing_type=postgresql.UUID(), nullable=False
    )


def downgrade() -> None:
    op.drop_index(
        "uq_consolidated_return_allocation_ledger_entry_id",
        table_name="consolidated_payment_return_allocation_evidence",
    )
    op.drop_table("consolidated_payment_return_allocation_evidence")
    _drop_billing_account_result("payment_reversals")
    _drop_billing_account_result("payment_refunds")
