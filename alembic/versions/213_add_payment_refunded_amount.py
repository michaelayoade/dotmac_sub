"""Add payments.refunded_amount (running refund total).

``Payment.amount`` stays the gross captured figure; refunds are recorded as
separate ``ledger_entries`` rows (source='refund'). Consumers that post net cash
(the ERP GL sync) otherwise had to sum the ledger per payment. This persists the
running total on the payment, maintained by the refund flow, and backfills it
from the existing refund ledger entries.

Revision ID: 213_add_payment_refunded_amount
Revises: 212_crm_invoice_idempotency
Create Date: 2026-07-06
"""

import sqlalchemy as sa
from sqlalchemy import inspect, text

from alembic import op

revision = "213_add_payment_refunded_amount"
down_revision = "212_add_forwarding_observations"
branch_labels = None
depends_on = None

_TABLE = "payments"
_COLUMN = "refunded_amount"


def _has_column(inspector, table: str, column: str) -> bool:
    if table not in inspector.get_table_names():
        return False
    return any(c["name"] == column for c in inspector.get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE not in inspector.get_table_names() or _has_column(
        inspector, _TABLE, _COLUMN
    ):
        return

    op.add_column(
        _TABLE,
        sa.Column(
            _COLUMN,
            sa.Numeric(12, 2),
            nullable=False,
            server_default="0",
        ),
    )
    # Backfill the running total from the refund ledger entries (positive
    # amounts, subtracted from the gross elsewhere). Use a single aggregate pass
    # over ledger_entries; the per-payment correlated form times out on prod data.
    bind.execute(text("SET LOCAL statement_timeout = 0"))
    bind.execute(
        text(
            "UPDATE payments p SET refunded_amount = refunds.total "
            "FROM ("
            "SELECT le.payment_id, SUM(le.amount) AS total "
            "FROM ledger_entries le "
            "WHERE le.source = 'refund' AND le.payment_id IS NOT NULL "
            "GROUP BY le.payment_id"
            ") refunds "
            "WHERE refunds.payment_id = p.id"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _has_column(inspector, _TABLE, _COLUMN):
        op.drop_column(_TABLE, _COLUMN)
