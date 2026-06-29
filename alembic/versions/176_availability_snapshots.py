"""Add availability_snapshots table.

Daily rolled-up availability per infrastructure element (device / pop_site /
pon_port), written by a Celery task, so the performance dashboard can chart
availability trends without re-merging the whole alert history on each render.
See INFRASTRUCTURE_SLA_PERFORMANCE.md Phase 2.

Revision ID: 176_availability_snapshots
Revises: 175_merge_invoice_line_and_usage_heads
Create Date: 2026-06-26
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "176_availability_snapshots"
down_revision = "175_merge_invoice_line_and_usage_heads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "availability_snapshots" in inspector.get_table_names():
        return
    op.create_table(
        "availability_snapshots",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("element_type", sa.String(length=20), nullable=False),
        sa.Column("element_id", UUID(as_uuid=True), nullable=False),
        sa.Column("snapshot_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("uptime_percent", sa.Float(), nullable=True),
        sa.Column("downtime_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("window_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("incident_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("affected_subscribers_peak", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint(
            "element_type",
            "element_id",
            "snapshot_date",
            name="uq_availability_snapshots_element_day",
        ),
    )
    op.create_index(
        "ix_availability_snapshots_type_date",
        "availability_snapshots",
        ["element_type", "snapshot_date"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "availability_snapshots" not in inspector.get_table_names():
        return
    op.drop_index(
        "ix_availability_snapshots_type_date",
        table_name="availability_snapshots",
    )
    op.drop_table("availability_snapshots")
