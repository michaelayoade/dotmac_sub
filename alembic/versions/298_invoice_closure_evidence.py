"""Add exact invoice void and write-off closure evidence.

Revision ID: 298_invoice_closure_evidence
Revises: 297_payment_settlement_evidence
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "298_invoice_closure_evidence"
down_revision = "297_payment_settlement_evidence"
branch_labels = None
depends_on = None

_closure_type = postgresql.ENUM(
    "void",
    "write_off",
    name="invoiceclosuretype",
    create_type=False,
)
_closure_origin = postgresql.ENUM(
    "manual",
    "system",
    "historical_reconciliation",
    name="invoiceclosureorigin",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    _closure_type.create(bind, checkfirst=True)
    _closure_origin.create(bind, checkfirst=True)
    op.create_table(
        "invoice_closures",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True
        ),
        sa.Column("invoice_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("closure_type", _closure_type, nullable=False),
        sa.Column("origin", _closure_origin, nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("receivable_before", sa.Numeric(12, 2), nullable=False),
        sa.Column(
            "receivable_after",
            sa.Numeric(12, 2),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "payments_applied",
            sa.Numeric(12, 2),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "credits_applied",
            sa.Numeric(12, 2),
            nullable=False,
            server_default="0",
        ),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("preview_fingerprint", sa.String(64), nullable=True),
        sa.Column("idempotency_key", sa.String(120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "amount >= 0", name="ck_invoice_closures_amount_nonnegative"
        ),
        sa.CheckConstraint(
            "receivable_before >= 0",
            name="ck_invoice_closures_receivable_before_nonnegative",
        ),
        sa.CheckConstraint(
            "receivable_after >= 0",
            name="ck_invoice_closures_receivable_after_nonnegative",
        ),
        sa.ForeignKeyConstraint(["invoice_id"], ["invoices.id"], ondelete="RESTRICT"),
    )
    op.create_index(
        "uq_invoice_closures_invoice_id",
        "invoice_closures",
        ["invoice_id"],
        unique=True,
    )
    op.create_index(
        "uq_invoice_closures_idempotency_key",
        "invoice_closures",
        ["idempotency_key"],
        unique=True,
    )
    op.create_table(
        "invoice_closure_ledger_evidence",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True
        ),
        sa.Column("closure_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ledger_entry_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "reverses_ledger_entry_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["closure_id"], ["invoice_closures.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["ledger_entry_id"], ["ledger_entries.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["reverses_ledger_entry_id"],
            ["ledger_entries.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "closure_id",
            "ledger_entry_id",
            name="uq_invoice_closure_evidence_closure_ledger",
        ),
    )
    op.create_index(
        "uq_invoice_closure_evidence_ledger_entry_id",
        "invoice_closure_ledger_evidence",
        ["ledger_entry_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_invoice_closure_evidence_ledger_entry_id",
        table_name="invoice_closure_ledger_evidence",
    )
    op.drop_table("invoice_closure_ledger_evidence")
    op.drop_index("uq_invoice_closures_idempotency_key", table_name="invoice_closures")
    op.drop_index("uq_invoice_closures_invoice_id", table_name="invoice_closures")
    op.drop_table("invoice_closures")
    bind = op.get_bind()
    _closure_origin.drop(bind, checkfirst=True)
    _closure_type.drop(bind, checkfirst=True)
