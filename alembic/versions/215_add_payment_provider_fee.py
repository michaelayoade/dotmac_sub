"""Add payments.provider_fee (gateway fee withheld from settlement).

``Payment.amount`` stays the gross the customer was charged; the payment gateway
settles ``amount - provider_fee`` to the bank (Paystack ``fees``, Flutterwave
``app_fee``). Persisting the fee lets the ERP GL sync split the receipt journal
(Dr Bank net / Dr bank-charges / Cr AR gross) so bank reconciliation ties. New
payments capture it from the signature-verified webhook; historical rows keep 0.

Revision ID: 215_add_payment_provider_fee
Revises: 214_add_ont_signal_observations
Create Date: 2026-07-06
"""

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "215_add_payment_provider_fee"
down_revision = "214_add_ont_signal_observations"
branch_labels = None
depends_on = None

_TABLE = "payments"
_COLUMN = "provider_fee"


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


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _has_column(inspector, _TABLE, _COLUMN):
        op.drop_column(_TABLE, _COLUMN)
