"""Add ip_pool_utilization_snapshots table.

Live IP-pool utilization is computed on demand (current counts only). This
table stores periodic point-in-time snapshots — written by a Celery task —
so the admin pool detail can chart utilization over time instead of only the
instantaneous bar.

Revision ID: 161_ip_pool_utilization_snapshots
Revises: 160_add_invoice_written_off_status
Create Date: 2026-06-19
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "161_ip_pool_utilization_snapshots"
down_revision = "160_add_invoice_written_off_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "ip_pool_utilization_snapshots" in inspector.get_table_names():
        return
    op.create_table(
        "ip_pool_utilization_snapshots",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "pool_id",
            UUID(as_uuid=True),
            sa.ForeignKey("ip_pools.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "captured_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reserved", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("available", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("percent", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index(
        "ix_ip_pool_utilization_snapshots_pool_id",
        "ip_pool_utilization_snapshots",
        ["pool_id"],
    )
    op.create_index(
        "ix_ip_pool_util_snap_pool_time",
        "ip_pool_utilization_snapshots",
        ["pool_id", "captured_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "ip_pool_utilization_snapshots" not in inspector.get_table_names():
        return
    op.drop_index(
        "ix_ip_pool_util_snap_pool_time",
        table_name="ip_pool_utilization_snapshots",
    )
    op.drop_index(
        "ix_ip_pool_utilization_snapshots_pool_id",
        table_name="ip_pool_utilization_snapshots",
    )
    op.drop_table("ip_pool_utilization_snapshots")
