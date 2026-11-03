"""IP pool labels (purpose, site_ref) and the NAS pool bindings table.

Phase A of the pool relabelling for the IPAM cutover: labels only. Additive and
nullable; no backfill and no data writes. Allocation, assignment and RADIUS are
unchanged. ``purpose`` / ``ck_ip_pools_purpose`` / ``site_ref`` match the
Starter dotmac-ipam pool schema so the values copy across. The pool <-> NAS link
lives in ``nas_pool_bindings`` on the network/access side, not on the pool.

Revision ID: 703_ip_pool_labels
Revises: 702_projection_secret_requests
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "703_ip_pool_labels"
down_revision: str | None = "702_projection_secret_requests"
branch_labels = None
depends_on = None

_PURPOSE_CHECK = (
    "purpose IS NULL OR purpose IN "
    "('customer', 'customer_nat', 'infrastructure', 'management')"
)


def upgrade() -> None:
    op.add_column("ip_pools", sa.Column("purpose", sa.String(length=20), nullable=True))
    op.add_column(
        "ip_pools", sa.Column("site_ref", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.create_check_constraint("ck_ip_pools_purpose", "ip_pools", _PURPOSE_CHECK)
    op.create_table(
        "nas_pool_bindings",
        sa.Column(
            "nas_device_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("nas_devices.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "pool_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ip_pools.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("nas_device_id", "pool_id"),
    )
    op.create_index("ix_nas_pool_bindings_pool_id", "nas_pool_bindings", ["pool_id"])


def downgrade() -> None:
    op.drop_index("ix_nas_pool_bindings_pool_id", table_name="nas_pool_bindings")
    op.drop_table("nas_pool_bindings")
    op.drop_constraint("ck_ip_pools_purpose", "ip_pools", type_="check")
    op.drop_column("ip_pools", "site_ref")
    op.drop_column("ip_pools", "purpose")
