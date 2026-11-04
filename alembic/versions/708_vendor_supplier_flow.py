"""Enable the new Sub-owned vendor supplier delivery flow.

Revision ID: 708_vendor_supplier_flow
Revises: 707_inbox_contact_sensitive_permissions
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "708_vendor_supplier_flow"
down_revision: str | None = "707_inbox_contact_sensitive_permissions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ownership = sa.table(
        "sync_flow_ownership",
        sa.column("id", sa.Uuid()),
        sa.column("flow", sa.String()),
        sa.column("owner", sa.String()),
        sa.column("updated_at", sa.DateTime(timezone=True)),
        sa.column("updated_by", sa.String()),
    )
    op.bulk_insert(
        ownership,
        [
            {
                "id": uuid.uuid4(),
                "flow": "vendor_supplier",
                "owner": "sub",
                "updated_at": datetime.now(UTC),
                "updated_by": "migration:708_vendor_supplier_flow",
            }
        ],
    )


def downgrade() -> None:
    op.execute("DELETE FROM sync_flow_ownership WHERE flow = 'vendor_supplier'")
