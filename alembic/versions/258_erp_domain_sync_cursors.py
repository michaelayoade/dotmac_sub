"""Add durable cursors for Sub operational context pushed to ERP.

Revision ID: 258_erp_domain_sync_cursors
Revises: 257_material_warehouse_serials
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "258_erp_domain_sync_cursors"
down_revision = "257_material_warehouse_serials"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid_type = (
        postgresql.UUID(as_uuid=True)
        if op.get_bind().dialect.name == "postgresql"
        else sa.String(length=36)
    )
    op.create_table(
        "erp_domain_sync_cursors",
        sa.Column("domain", sa.String(length=40), primary_key=True),
        sa.Column("watermark_at", sa.DateTime(timezone=True)),
        sa.Column("watermark_id", uuid_type),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("erp_domain_sync_cursors")
