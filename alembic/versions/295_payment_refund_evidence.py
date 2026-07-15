"""Add exact evidence for completed payment refunds.

Revision ID: 295_payment_refund_evidence
Revises: 294_credit_note_lifecycle_evidence
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "295_payment_refund_evidence"
down_revision = "294_credit_note_lifecycle_evidence"
branch_labels = None
depends_on = None

_origin = postgresql.ENUM(
    "manual",
    "provider_event",
    name="paymentrefundorigin",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    _origin.create(bind, checkfirst=True)
    op.add_column(
        "payment_provider_events",
        sa.Column("amount", sa.Numeric(12, 2), nullable=True),
    )
    op.add_column(
        "payment_provider_events",
        sa.Column("currency", sa.String(3), nullable=True),
    )
    op.create_table(
        "payment_refunds",
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
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("preview_fingerprint", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount > 0", name="ck_payment_refunds_amount_positive"),
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
    op.create_index(
        "uq_payment_refunds_ledger_entry_id",
        "payment_refunds",
        ["ledger_entry_id"],
        unique=True,
    )
    op.create_index(
        "uq_payment_refunds_credit_consumption_ledger_entry_id",
        "payment_refunds",
        ["credit_consumption_ledger_entry_id"],
        unique=True,
    )
    op.create_index(
        "uq_payment_refunds_provider_event_id",
        "payment_refunds",
        ["provider_event_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_payment_refunds_provider_event_id", table_name="payment_refunds")
    op.drop_index(
        "uq_payment_refunds_credit_consumption_ledger_entry_id",
        table_name="payment_refunds",
    )
    op.drop_index("uq_payment_refunds_ledger_entry_id", table_name="payment_refunds")
    op.drop_table("payment_refunds")
    op.drop_column("payment_provider_events", "currency")
    op.drop_column("payment_provider_events", "amount")
    _origin.drop(op.get_bind(), checkfirst=True)
