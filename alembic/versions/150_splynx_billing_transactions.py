"""Faithful mirror of Splynx billing_transactions: splynx_billing_transactions.

Imports the granular Splynx transaction ledger (credit/debit movements whose
net per customer == customer_billing.deposit) so financial history is at parity
with Splynx and the deposit reconciles locally. Kept separate from
ledger_entries to avoid double-counting the invoice/payment-derived entries.

Revision ID: 150_splynx_billing_transactions
Revises: 149_crm_sync_failures
Create Date: 2026-06-13
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "150_splynx_billing_transactions"
down_revision = "149_crm_sync_failures"
branch_labels = None
depends_on = None

_TABLE = "splynx_billing_transactions"


def upgrade() -> None:
    bind = op.get_bind()
    if inspect(bind).has_table(_TABLE):
        return
    op.create_table(
        _TABLE,
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("splynx_transaction_id", sa.Integer(), nullable=False),
        sa.Column("splynx_customer_id", sa.Integer(), nullable=False),
        sa.Column("subscriber_id", UUID(as_uuid=True)),
        sa.Column("entry_type", sa.String(10), nullable=False),
        sa.Column("amount", sa.Numeric(16, 2), nullable=False, server_default="0"),
        sa.Column("category_id", sa.Integer()),
        sa.Column("category_name", sa.String(120)),
        sa.Column("description", sa.Text()),
        sa.Column("transaction_date", sa.Date()),
        sa.Column("period_from", sa.Date()),
        sa.Column("period_to", sa.Date()),
        sa.Column("splynx_invoice_id", sa.Integer()),
        sa.Column("splynx_payment_id", sa.Integer()),
        sa.Column("splynx_credit_note_id", sa.Integer()),
        sa.Column("service_id", sa.Integer()),
        sa.Column("service_type", sa.String(40)),
        sa.Column("source", sa.String(40)),
        sa.Column("deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        f"ix_{_TABLE}_splynx_transaction_id",
        _TABLE,
        ["splynx_transaction_id"],
        unique=True,
    )
    op.create_index(f"ix_{_TABLE}_splynx_customer_id", _TABLE, ["splynx_customer_id"])
    op.create_index(f"ix_{_TABLE}_subscriber_id", _TABLE, ["subscriber_id"])
    op.create_index(f"ix_{_TABLE}_transaction_date", _TABLE, ["transaction_date"])
    op.create_index(f"ix_{_TABLE}_deleted", _TABLE, ["deleted"])


def downgrade() -> None:
    bind = op.get_bind()
    if inspect(bind).has_table(_TABLE):
        op.drop_table(_TABLE)
