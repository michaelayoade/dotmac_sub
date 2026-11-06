"""Enable the new Sub-owned vendor supplier delivery flow.

Revision ID: 708_vendor_supplier_flow
Revises: 707_inbox_contact_sensitive_permissions
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "708_vendor_supplier_flow"
down_revision: str | None = "707_inbox_contact_sensitive_permissions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        return

    ownership = sa.table(
        "sync_flow_ownership",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("flow", sa.String()),
        sa.column("owner", sa.String()),
        sa.column("updated_at", sa.DateTime(timezone=True)),
        sa.column("updated_by", sa.String()),
    )
    now = datetime.now(UTC)
    statement = postgresql.insert(ownership).values(
        id=uuid.uuid4(),
        flow="vendor_supplier",
        owner="sub",
        updated_at=now,
        updated_by="migration:708_vendor_supplier_flow",
    )
    op.execute(
        statement.on_conflict_do_update(
            index_elements=[ownership.c.flow],
            set_={
                "owner": "sub",
                "updated_at": now,
                "updated_by": "migration:708_vendor_supplier_flow",
            },
        )
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        return
    ownership = sa.table(
        "sync_flow_ownership",
        sa.column("flow", sa.String()),
    )
    op.execute(ownership.delete().where(ownership.c.flow == "vendor_supplier"))
