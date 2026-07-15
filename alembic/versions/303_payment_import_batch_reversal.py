"""Add durable provenance and exact imported-payment batch reversal evidence.

Revision ID: 303_payment_import_batch_reversal
Revises: 302_plan_change_confirmation_evidence
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "303_payment_import_batch_reversal"
down_revision = "302_plan_change_confirmation_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "payments",
        sa.Column("import_run_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_payments_import_run_id",
        "payments",
        "import_runs",
        ["import_run_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_payments_import_run_id", "payments", ["import_run_id"], unique=False
    )

    op.add_column(
        "import_run_rows",
        sa.Column("payment_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "import_run_rows",
        sa.Column("record_created", sa.Boolean(), nullable=True),
    )
    op.create_foreign_key(
        "fk_import_run_rows_payment_id",
        "import_run_rows",
        "payments",
        ["payment_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_import_run_rows_payment_id",
        "import_run_rows",
        ["payment_id"],
        unique=False,
    )

    op.create_table(
        "payment_import_batch_reversals",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True
        ),
        sa.Column("import_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("preview_fingerprint", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(120), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("preview_snapshot", sa.JSON(), nullable=False),
        sa.Column("reversed_payment_count", sa.Integer(), nullable=False),
        sa.Column("skipped_reused_count", sa.Integer(), nullable=False),
        sa.Column("confirmed_by", sa.String(120), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["import_run_id"], ["import_runs.id"], ondelete="RESTRICT"
        ),
    )
    op.create_index(
        "uq_payment_import_batch_reversals_run_id",
        "payment_import_batch_reversals",
        ["import_run_id"],
        unique=True,
    )
    op.create_index(
        "uq_payment_import_batch_reversals_idempotency_key",
        "payment_import_batch_reversals",
        ["idempotency_key"],
        unique=True,
    )

    op.create_table(
        "payment_import_batch_reversal_items",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True
        ),
        sa.Column("batch_reversal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("import_run_row_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "payment_settlement_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("payment_reversal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ledger_entry_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "credit_consumption_ledger_entry_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("source_snapshot", sa.JSON(), nullable=False),
        sa.Column("result_snapshot", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["batch_reversal_id"],
            ["payment_import_batch_reversals.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["import_run_row_id"], ["import_run_rows.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["payment_id"], ["payments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["payment_settlement_id"],
            ["payment_settlements.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["payment_reversal_id"],
            ["payment_reversals.id"],
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
        sa.UniqueConstraint(
            "batch_reversal_id",
            "payment_id",
            name="uq_payment_import_batch_reversal_item_payment",
        ),
    )
    for index_name, column_name in (
        (
            "uq_payment_import_batch_reversal_items_run_row_id",
            "import_run_row_id",
        ),
        (
            "uq_payment_import_batch_reversal_items_reversal_id",
            "payment_reversal_id",
        ),
        (
            "uq_payment_import_batch_reversal_items_ledger_entry_id",
            "ledger_entry_id",
        ),
        (
            "uq_payment_import_batch_reversal_items_consumption_entry_id",
            "credit_consumption_ledger_entry_id",
        ),
    ):
        op.create_index(
            index_name,
            "payment_import_batch_reversal_items",
            [column_name],
            unique=True,
        )


def downgrade() -> None:
    for index_name in (
        "uq_payment_import_batch_reversal_items_consumption_entry_id",
        "uq_payment_import_batch_reversal_items_ledger_entry_id",
        "uq_payment_import_batch_reversal_items_reversal_id",
        "uq_payment_import_batch_reversal_items_run_row_id",
    ):
        op.drop_index(index_name, table_name="payment_import_batch_reversal_items")
    op.drop_table("payment_import_batch_reversal_items")
    op.drop_index(
        "uq_payment_import_batch_reversals_idempotency_key",
        table_name="payment_import_batch_reversals",
    )
    op.drop_index(
        "uq_payment_import_batch_reversals_run_id",
        table_name="payment_import_batch_reversals",
    )
    op.drop_table("payment_import_batch_reversals")
    op.drop_index("ix_import_run_rows_payment_id", table_name="import_run_rows")
    op.drop_constraint(
        "fk_import_run_rows_payment_id", "import_run_rows", type_="foreignkey"
    )
    op.drop_column("import_run_rows", "record_created")
    op.drop_column("import_run_rows", "payment_id")
    op.drop_index("ix_payments_import_run_id", table_name="payments")
    op.drop_constraint("fk_payments_import_run_id", "payments", type_="foreignkey")
    op.drop_column("payments", "import_run_id")
