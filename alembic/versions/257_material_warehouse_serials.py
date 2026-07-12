"""Add first-class warehouse and serial selections to material requests.

Revision ID: 257_material_warehouse_serials
Revises: 256_vendor_purchase_invoices
Create Date: 2026-07-12
"""

import sqlalchemy as sa

from alembic import op

revision = "257_material_warehouse_serials"
down_revision = "256_vendor_purchase_invoices"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "field_material_requests",
        sa.Column("source_warehouse_code", sa.String(length=100)),
    )
    op.add_column(
        "field_material_request_items",
        sa.Column("serial_numbers", sa.JSON()),
    )


def downgrade() -> None:
    op.drop_column("field_material_request_items", "serial_numbers")
    op.drop_column("field_material_requests", "source_warehouse_code")
