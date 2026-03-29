"""Add Splynx migration columns for traceability and extended data.

Adds splynx_customer_id, splynx_invoice_id, splynx_payment_id,
splynx_monitoring_id columns plus invoice.is_sent, invoice.added_by_id,
and payment.receipt_number.

Revision ID: s7t8u9v0w2y3
Revises: r5s6t7u8v9w0
Create Date: 2026-02-17 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "s7t8u9v0w2y3"
down_revision: str | None = "r5s6t7u8v9w0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_exists(inspector: sa.Inspector, table: str, column: str) -> bool:
    """Check if a column exists on a table."""
    columns = [c["name"] for c in inspector.get_columns(table)]
    return column in columns


def _index_exists(inspector: sa.Inspector, table: str, index_name: str) -> bool:
    """Check if an index exists on a table."""
    indexes = [idx["name"] for idx in inspector.get_indexes(table)]
    return index_name in indexes


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # -- subscribers.splynx_customer_id --
    if not _column_exists(inspector, "subscribers", "splynx_customer_id"):
        op.add_column(
            "subscribers",
            sa.Column("splynx_customer_id", sa.Integer(), nullable=True),
        )
    if not _index_exists(inspector, "subscribers", "ix_subscribers_splynx_customer_id"):
        op.create_index(
            "ix_subscribers_splynx_customer_id",
            "subscribers",
            ["splynx_customer_id"],
            unique=True,
            postgresql_where=sa.text("splynx_customer_id IS NOT NULL"),
        )

    # -- invoices.is_sent --
    if not _column_exists(inspector, "invoices", "is_sent"):
        op.add_column(
            "invoices",
            sa.Column(
                "is_sent", sa.Boolean(), nullable=True, server_default=sa.text("false")
            ),
        )

    # -- invoices.added_by_id --
    if not _column_exists(inspector, "invoices", "added_by_id"):
        op.add_column(
            "invoices",
            sa.Column(
                "added_by_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("subscribers.id"),
                nullable=True,
            ),
        )

    # -- invoices.splynx_invoice_id --
    if not _column_exists(inspector, "invoices", "splynx_invoice_id"):
        op.add_column(
            "invoices",
            sa.Column("splynx_invoice_id", sa.Integer(), nullable=True),
        )
    if not _index_exists(inspector, "invoices", "ix_invoices_splynx_invoice_id"):
        op.create_index(
            "ix_invoices_splynx_invoice_id",
            "invoices",
            ["splynx_invoice_id"],
            unique=True,
            postgresql_where=sa.text("splynx_invoice_id IS NOT NULL"),
        )

    # -- payments.receipt_number --
    if not _column_exists(inspector, "payments", "receipt_number"):
        op.add_column(
            "payments",
            sa.Column("receipt_number", sa.String(120), nullable=True),
        )

    # -- payments.splynx_payment_id --
    if not _column_exists(inspector, "payments", "splynx_payment_id"):
        op.add_column(
            "payments",
            sa.Column("splynx_payment_id", sa.Integer(), nullable=True),
        )
    if not _index_exists(inspector, "payments", "ix_payments_splynx_payment_id"):
        op.create_index(
            "ix_payments_splynx_payment_id",
            "payments",
            ["splynx_payment_id"],
            unique=True,
            postgresql_where=sa.text("splynx_payment_id IS NOT NULL"),
        )

    # -- network_devices.splynx_monitoring_id --
    if not _column_exists(inspector, "network_devices", "splynx_monitoring_id"):
        op.add_column(
            "network_devices",
            sa.Column("splynx_monitoring_id", sa.Integer(), nullable=True),
        )
    if not _index_exists(
        inspector, "network_devices", "ix_network_devices_splynx_monitoring_id"
    ):
        op.create_index(
            "ix_network_devices_splynx_monitoring_id",
            "network_devices",
            ["splynx_monitoring_id"],
            unique=True,
            postgresql_where=sa.text("splynx_monitoring_id IS NOT NULL"),
        )


def downgrade() -> None:
    # Drop in reverse order
    op.drop_index(
        "ix_network_devices_splynx_monitoring_id",
        table_name="network_devices",
        if_exists=True,
    )
    op.drop_column("network_devices", "splynx_monitoring_id")

    op.drop_index(
        "ix_payments_splynx_payment_id", table_name="payments", if_exists=True
    )
    op.drop_column("payments", "splynx_payment_id")
    op.drop_column("payments", "receipt_number")

    op.drop_index(
        "ix_invoices_splynx_invoice_id", table_name="invoices", if_exists=True
    )
    op.drop_column("invoices", "splynx_invoice_id")
    op.drop_column("invoices", "added_by_id")
    op.drop_column("invoices", "is_sent")

    op.drop_index(
        "ix_subscribers_splynx_customer_id", table_name="subscribers", if_exists=True
    )
    op.drop_column("subscribers", "splynx_customer_id")
