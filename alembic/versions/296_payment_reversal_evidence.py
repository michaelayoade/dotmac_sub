"""Separate chargeback/payment reversal state and exact evidence.

Revision ID: 296_payment_reversal_evidence
Revises: 295_payment_refund_evidence
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "296_payment_reversal_evidence"
down_revision = "295_payment_refund_evidence"
branch_labels = None
depends_on = None

_origin = postgresql.ENUM(
    "manual",
    "provider_event",
    name="paymentreversalorigin",
    create_type=False,
)
_financial_effect = postgresql.ENUM(
    "none",
    "refund_confirmed",
    "reversal_confirmed",
    name="paymentprovidereventfinancialeffect",
    create_type=False,
)


def upgrade() -> None:
    op.execute(
        "ALTER TYPE paymentstatus ADD VALUE IF NOT EXISTS 'reversed' "
        "AFTER 'partially_refunded'"
    )
    op.execute("ALTER TYPE webhookeventtype ADD VALUE IF NOT EXISTS 'payment_reversed'")
    bind = op.get_bind()
    _origin.create(bind, checkfirst=True)
    _financial_effect.create(bind, checkfirst=True)
    op.add_column(
        "payment_provider_events",
        sa.Column(
            "financial_effect",
            _financial_effect,
            nullable=False,
            server_default="none",
        ),
    )
    op.create_table(
        "payment_reversals",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True
        ),
        sa.Column("payment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider_event_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("ledger_entry_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "credit_consumption_ledger_entry_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("origin", _origin, nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("preview_fingerprint", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount > 0", name="ck_payment_reversals_amount_positive"),
        sa.ForeignKeyConstraint(["payment_id"], ["payments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["provider_event_id"],
            ["payment_provider_events.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["ledger_entry_id"], ["ledger_entries.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["credit_consumption_ledger_entry_id"],
            ["ledger_entries.id"],
            ondelete="RESTRICT",
        ),
    )
    for index_name, column_name in (
        ("uq_payment_reversals_payment_id", "payment_id"),
        ("uq_payment_reversals_provider_event_id", "provider_event_id"),
        ("uq_payment_reversals_ledger_entry_id", "ledger_entry_id"),
        (
            "uq_payment_reversals_credit_consumption_ledger_entry_id",
            "credit_consumption_ledger_entry_id",
        ),
    ):
        op.create_index(
            index_name,
            "payment_reversals",
            [column_name],
            unique=True,
        )


def downgrade() -> None:
    for index_name in (
        "uq_payment_reversals_credit_consumption_ledger_entry_id",
        "uq_payment_reversals_ledger_entry_id",
        "uq_payment_reversals_provider_event_id",
        "uq_payment_reversals_payment_id",
    ):
        op.drop_index(index_name, table_name="payment_reversals")
    op.drop_table("payment_reversals")
    op.drop_column("payment_provider_events", "financial_effect")
    _financial_effect.drop(op.get_bind(), checkfirst=True)
    _origin.drop(op.get_bind(), checkfirst=True)
    # PostgreSQL enum values cannot be removed safely in-place. The dormant
    # paymentstatus/webhookeventtype values remain after downgrade.
