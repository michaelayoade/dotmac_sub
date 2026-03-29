"""add finance persistence tables

Revision ID: f6a7b8c9d0e1
Revises: 1c0efbd4db66
Create Date: 2026-02-25 02:40:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "f6a7b8c9d0e1"
down_revision: Union[str, Sequence[str], None] = "1c0efbd4db66"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "invoices",
        sa.Column("is_proforma", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.create_index("ix_invoices_is_proforma", "invoices", ["is_proforma"], unique=False)

    op.create_table(
        "billing_run_schedules",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("run_day", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("run_time", sa.String(length=8), nullable=False, server_default="02:00"),
        sa.Column("timezone", sa.String(length=64), nullable=False, server_default="UTC"),
        sa.Column("billing_cycle", sa.String(length=40), nullable=False, server_default="monthly"),
        sa.Column("partner_ids", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "bank_reconciliation_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("date_range", sa.String(length=20), nullable=True),
        sa.Column("handler", sa.String(length=120), nullable=True),
        sa.Column("statement_rows", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("imported_rows", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("unmatched_rows", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("system_payment_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("statement_total", sa.Numeric(14, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("payment_total", sa.Numeric(14, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("difference_total", sa.Numeric(14, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_bank_reconciliation_runs_created_at",
        "bank_reconciliation_runs",
        ["created_at"],
        unique=False,
    )

    op.create_table(
        "bank_reconciliation_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("item_type", sa.String(length=20), nullable=False, server_default="unmatched"),
        sa.Column("reference", sa.String(length=255), nullable=True),
        sa.Column("file_name", sa.String(length=255), nullable=True),
        sa.Column("count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["bank_reconciliation_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_bank_reconciliation_items_run_id",
        "bank_reconciliation_items",
        ["run_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_bank_reconciliation_items_run_id", table_name="bank_reconciliation_items")
    op.drop_table("bank_reconciliation_items")

    op.drop_index("ix_bank_reconciliation_runs_created_at", table_name="bank_reconciliation_runs")
    op.drop_table("bank_reconciliation_runs")

    op.drop_table("billing_run_schedules")

    op.drop_index("ix_invoices_is_proforma", table_name="invoices")
    op.drop_column("invoices", "is_proforma")
