"""Add refreshed ERP payment observations to vendor purchase invoices.

Revision ID: 372_vendor_payment_projection
Revises: 371_retire_coarse_reports_permissions
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "372_vendor_payment_projection"
down_revision = "371_retire_coarse_reports_permissions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "vendor_purchase_invoices",
        sa.Column("erp_purchase_invoice_creation_status", sa.String(40)),
    )
    op.execute(
        sa.text(
            "UPDATE vendor_purchase_invoices "
            "SET erp_purchase_invoice_creation_status = erp_purchase_invoice_status, "
            "erp_purchase_invoice_status = NULL "
            "WHERE erp_purchase_invoice_status IS NOT NULL"
        )
    )
    op.add_column(
        "vendor_purchase_invoices",
        sa.Column("erp_purchase_invoice_total_amount", sa.Numeric(20, 6)),
    )
    op.add_column(
        "vendor_purchase_invoices",
        sa.Column("erp_purchase_invoice_amount_paid", sa.Numeric(20, 6)),
    )
    op.add_column(
        "vendor_purchase_invoices",
        sa.Column("erp_purchase_invoice_balance_due", sa.Numeric(20, 6)),
    )
    op.add_column(
        "vendor_purchase_invoices",
        sa.Column(
            "erp_purchase_invoice_status_observed_at",
            sa.DateTime(timezone=True),
        ),
    )
    op.add_column(
        "vendor_purchase_invoices",
        sa.Column(
            "erp_purchase_invoice_status_source_updated_at",
            sa.DateTime(timezone=True),
        ),
    )
    op.add_column(
        "vendor_purchase_invoices",
        sa.Column("erp_purchase_invoice_status_error", sa.String(500)),
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE vendor_purchase_invoices "
            "SET erp_purchase_invoice_status = COALESCE("
            "erp_purchase_invoice_status, erp_purchase_invoice_creation_status)"
        )
    )
    op.drop_column("vendor_purchase_invoices", "erp_purchase_invoice_status_error")
    op.drop_column(
        "vendor_purchase_invoices",
        "erp_purchase_invoice_status_source_updated_at",
    )
    op.drop_column(
        "vendor_purchase_invoices", "erp_purchase_invoice_status_observed_at"
    )
    op.drop_column("vendor_purchase_invoices", "erp_purchase_invoice_balance_due")
    op.drop_column("vendor_purchase_invoices", "erp_purchase_invoice_amount_paid")
    op.drop_column("vendor_purchase_invoices", "erp_purchase_invoice_total_amount")
    op.drop_column("vendor_purchase_invoices", "erp_purchase_invoice_creation_status")
