"""Add native vendor purchase invoices.

Revision ID: 256_vendor_purchase_invoices
Revises: 255_phase5_asset_inventory
Create Date: 2026-07-12
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "256_vendor_purchase_invoices"
down_revision = "255_phase5_asset_inventory"
branch_labels = None
depends_on = None


def _uuid_type() -> sa.types.TypeEngine:
    if op.get_bind().dialect.name == "postgresql":
        return postgresql.UUID(as_uuid=True)
    return sa.String(length=36)


def upgrade() -> None:
    uuid_type = _uuid_type()
    op.create_table(
        "vendor_purchase_invoices",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("invoice_number", sa.String(length=80)),
        sa.Column(
            "project_id",
            uuid_type,
            sa.ForeignKey("installation_projects.id"),
            nullable=False,
        ),
        sa.Column(
            "vendor_id", uuid_type, sa.ForeignKey("vendors.id"), nullable=False
        ),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="draft"),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="NGN"),
        sa.Column("tax_rate_percent", sa.Numeric(5, 2)),
        sa.Column("subtotal", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("tax_total", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("total", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "reviewed_by_system_user_id",
            uuid_type,
            sa.ForeignKey("system_users.id"),
        ),
        sa.Column("review_notes", sa.Text()),
        sa.Column(
            "created_by_system_user_id",
            uuid_type,
            sa.ForeignKey("system_users.id"),
        ),
        sa.Column(
            "attachment_stored_file_id",
            uuid_type,
            sa.ForeignKey("stored_files.id"),
        ),
        sa.Column("erp_purchase_order_id", sa.String(length=100)),
        sa.Column("erp_purchase_invoice_id", sa.String(length=100)),
        sa.Column("erp_purchase_invoice_status", sa.String(length=40)),
        sa.Column("erp_sync_error", sa.String(length=500)),
        sa.Column("erp_synced_at", sa.DateTime(timezone=True)),
        sa.Column("erp_attachment_synced_at", sa.DateTime(timezone=True)),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "project_id",
            "vendor_id",
            name="uq_vendor_purchase_invoice_project_vendor",
        ),
        sa.UniqueConstraint(
            "vendor_id",
            "invoice_number",
            name="uq_vendor_purchase_invoice_vendor_number",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'submitted', 'under_review', 'approved', "
            "'rejected', 'revision_requested')",
            name="ck_vendor_purchase_invoices_status",
        ),
    )
    for name, columns in (
        ("ix_vendor_purchase_invoices_invoice_number", ["invoice_number"]),
        ("ix_vendor_purchase_invoices_project_id", ["project_id"]),
        ("ix_vendor_purchase_invoices_vendor_id", ["vendor_id"]),
        ("ix_vendor_purchase_invoices_erp_purchase_order_id", ["erp_purchase_order_id"]),
        ("ix_vendor_purchase_invoices_erp_purchase_invoice_id", ["erp_purchase_invoice_id"]),
    ):
        op.create_index(name, "vendor_purchase_invoices", columns)

    op.create_table(
        "vendor_purchase_invoice_line_items",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column(
            "invoice_id",
            uuid_type,
            sa.ForeignKey("vendor_purchase_invoices.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("item_type", sa.String(length=80)),
        sa.Column("description", sa.Text()),
        sa.Column("quantity", sa.Numeric(12, 3), nullable=False, server_default="1"),
        sa.Column("unit_price", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("notes", sa.Text()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "quantity > 0", name="ck_vendor_purchase_invoice_line_quantity_positive"
        ),
    )
    op.create_index(
        "ix_vendor_purchase_invoice_line_items_invoice_id",
        "vendor_purchase_invoice_line_items",
        ["invoice_id"],
    )


def downgrade() -> None:
    op.drop_table("vendor_purchase_invoice_line_items")
    op.drop_table("vendor_purchase_invoices")
