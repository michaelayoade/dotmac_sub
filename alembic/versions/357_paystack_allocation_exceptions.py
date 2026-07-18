"""Add durable payment-allocation reconciliation exceptions.

Revision ID: 357_paystack_allocation_exceptions
Revises: 356_party_first_referral_capture
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "357_paystack_allocation_exceptions"
down_revision = "356_party_first_referral_capture"
branch_labels = None
depends_on = None
_TABLE = "payment_allocation_reconciliation_exceptions"


def upgrade() -> None:
    if _TABLE in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        _TABLE,
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("payment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("invoice_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("topup_intent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("provider_reference", sa.String(length=120), nullable=False),
        sa.Column("external_id", sa.String(length=120), nullable=False),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column(
            "status", sa.String(length=20), server_default="open", nullable=False
        ),
        sa.Column("error_type", sa.String(length=120), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="1", nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["invoice_id"], ["invoices.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["payment_id"], ["payments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["topup_intent_id"], ["topup_intents.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_payment_allocation_reconciliation_exceptions_key",
        "payment_allocation_reconciliation_exceptions",
        ["idempotency_key"],
        unique=True,
    )
    op.create_index(
        "ix_payment_allocation_reconciliation_exceptions_status_created",
        "payment_allocation_reconciliation_exceptions",
        ["status", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_payment_allocation_reconciliation_exceptions_payment",
        "payment_allocation_reconciliation_exceptions",
        ["payment_id"],
        unique=False,
    )


def downgrade() -> None:
    if _TABLE not in sa.inspect(op.get_bind()).get_table_names():
        return
    op.drop_index(
        "ix_payment_allocation_reconciliation_exceptions_payment",
        table_name="payment_allocation_reconciliation_exceptions",
    )
    op.drop_index(
        "ix_payment_allocation_reconciliation_exceptions_status_created",
        table_name="payment_allocation_reconciliation_exceptions",
    )
    op.drop_index(
        "uq_payment_allocation_reconciliation_exceptions_key",
        table_name="payment_allocation_reconciliation_exceptions",
    )
    op.drop_table("payment_allocation_reconciliation_exceptions")
