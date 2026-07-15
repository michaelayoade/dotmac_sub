"""Separate payment intent from confirmed settlement evidence.

Revision ID: 297_payment_settlement_evidence
Revises: 296_payment_reversal_evidence
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "297_payment_settlement_evidence"
down_revision = "296_payment_reversal_evidence"
branch_labels = None
depends_on = None

_origin = postgresql.ENUM(
    "manual",
    "provider_event",
    "system",
    name="paymentsettlementorigin",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    _origin.create(bind, checkfirst=True)
    op.add_column(
        "payments",
        sa.Column(
            "auto_allocate_on_settlement",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.add_column(
        "payments",
        sa.Column("creation_preview_fingerprint", sa.String(64), nullable=True),
    )
    op.add_column(
        "payment_allocations",
        sa.Column("ledger_entry_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "payment_allocations",
        sa.Column(
            "consumption_ledger_entry_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.add_column(
        "payment_allocations",
        sa.Column("preview_fingerprint", sa.String(64), nullable=True),
    )
    op.add_column(
        "payment_allocations",
        sa.Column("idempotency_key", sa.String(120), nullable=True),
    )
    op.create_foreign_key(
        "fk_payment_allocations_ledger_entry_id",
        "payment_allocations",
        "ledger_entries",
        ["ledger_entry_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "uq_payment_allocations_ledger_entry_id",
        "payment_allocations",
        ["ledger_entry_id"],
        unique=True,
    )
    op.create_foreign_key(
        "fk_payment_allocations_consumption_ledger_entry_id",
        "payment_allocations",
        "ledger_entries",
        ["consumption_ledger_entry_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "uq_payment_allocations_consumption_ledger_entry_id",
        "payment_allocations",
        ["consumption_ledger_entry_id"],
        unique=True,
    )
    op.create_index(
        "uq_payment_allocations_idempotency_key",
        "payment_allocations",
        ["idempotency_key"],
        unique=True,
    )
    op.create_table(
        "payment_settlements",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True
        ),
        sa.Column("payment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "unallocated_ledger_entry_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            "prepaid_ledger_entry_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("unallocated_amount", sa.Numeric(12, 2), nullable=False),
        sa.Column(
            "prepaid_amount",
            sa.Numeric(12, 2),
            nullable=False,
            server_default="0",
        ),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("origin", _origin, nullable=False),
        sa.Column("preview_fingerprint", sa.String(64), nullable=True),
        sa.Column("idempotency_key", sa.String(120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount > 0", name="ck_payment_settlements_amount_positive"),
        sa.CheckConstraint(
            "unallocated_amount >= 0",
            name="ck_payment_settlements_unallocated_nonnegative",
        ),
        sa.ForeignKeyConstraint(["payment_id"], ["payments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["unallocated_ledger_entry_id"],
            ["ledger_entries.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["prepaid_ledger_entry_id"],
            ["ledger_entries.id"],
            ondelete="RESTRICT",
        ),
    )
    for index_name, column_name in (
        ("uq_payment_settlements_payment_id", "payment_id"),
        ("uq_payment_settlements_idempotency_key", "idempotency_key"),
        (
            "uq_payment_settlements_unallocated_ledger_entry_id",
            "unallocated_ledger_entry_id",
        ),
        (
            "uq_payment_settlements_prepaid_ledger_entry_id",
            "prepaid_ledger_entry_id",
        ),
    ):
        op.create_index(
            index_name,
            "payment_settlements",
            [column_name],
            unique=True,
        )


def downgrade() -> None:
    for index_name in (
        "uq_payment_settlements_prepaid_ledger_entry_id",
        "uq_payment_settlements_unallocated_ledger_entry_id",
        "uq_payment_settlements_idempotency_key",
        "uq_payment_settlements_payment_id",
    ):
        op.drop_index(index_name, table_name="payment_settlements")
    op.drop_table("payment_settlements")
    op.drop_index(
        "uq_payment_allocations_idempotency_key",
        table_name="payment_allocations",
    )
    op.drop_index(
        "uq_payment_allocations_consumption_ledger_entry_id",
        table_name="payment_allocations",
    )
    op.drop_constraint(
        "fk_payment_allocations_consumption_ledger_entry_id",
        "payment_allocations",
        type_="foreignkey",
    )
    op.drop_index(
        "uq_payment_allocations_ledger_entry_id",
        table_name="payment_allocations",
    )
    op.drop_constraint(
        "fk_payment_allocations_ledger_entry_id",
        "payment_allocations",
        type_="foreignkey",
    )
    op.drop_column("payment_allocations", "idempotency_key")
    op.drop_column("payment_allocations", "preview_fingerprint")
    op.drop_column("payment_allocations", "consumption_ledger_entry_id")
    op.drop_column("payment_allocations", "ledger_entry_id")
    op.drop_column("payments", "creation_preview_fingerprint")
    op.drop_column("payments", "auto_allocate_on_settlement")
    _origin.drop(op.get_bind(), checkfirst=True)
