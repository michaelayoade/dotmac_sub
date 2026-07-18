"""Add exact evidence for prepaid use of settled payment credit.

Revision ID: 359_payment_prepaid_applications
Revises: 358_paystack_allocation_exceptions
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "359_payment_prepaid_applications"
down_revision = "358_paystack_allocation_exceptions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "payment_prepaid_applications",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("settlement_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subscription_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "credit_ledger_entry_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "debit_ledger_entry_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("entitlement_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "retired_allocation_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column(
            "historical_invoice_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("invoice_closure_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("origin", sa.String(length=32), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("preview_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column(
            "access_recheck_status",
            sa.String(length=24),
            nullable=False,
            server_default="not_required",
        ),
        sa.Column("access_recheck_error", sa.String(length=120), nullable=True),
        sa.Column("access_rechecked_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint(
            "amount > 0", name="ck_payment_prepaid_applications_amount_positive"
        ),
        sa.CheckConstraint(
            "period_end > period_start",
            name="ck_payment_prepaid_applications_period_order",
        ),
        sa.CheckConstraint(
            "origin IN ('historical_reconciliation', 'post_settlement')",
            name="ck_payment_prepaid_applications_origin",
        ),
        sa.CheckConstraint(
            "access_recheck_status IN "
            "('not_required', 'pending', 'completed', 'deferred')",
            name="ck_payment_prepaid_applications_access_status",
        ),
        sa.ForeignKeyConstraint(["payment_id"], ["payments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["settlement_id"], ["payment_settlements.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["subscribers.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"], ["subscriptions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["credit_ledger_entry_id"], ["ledger_entries.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["debit_ledger_entry_id"], ["ledger_entries.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["entitlement_id"], ["service_entitlements.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["retired_allocation_id"],
            ["payment_allocations.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["historical_invoice_id"], ["invoices.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["invoice_closure_id"], ["invoice_closures.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in (
        "payment_id",
        "settlement_id",
        "credit_ledger_entry_id",
        "debit_ledger_entry_id",
        "entitlement_id",
        "retired_allocation_id",
        "invoice_closure_id",
        "idempotency_key",
    ):
        op.create_index(
            f"uq_payment_prepaid_applications_{column}",
            "payment_prepaid_applications",
            [column],
            unique=True,
        )


def downgrade() -> None:
    op.drop_table("payment_prepaid_applications")
