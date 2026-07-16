"""Materialise the unified device projection table.

One row per network device across the OLT, core NetworkDevice, ONT and CPE
tables, with the operational status pre-derived. This is a rebuildable cache
owned solely by the ``network.device_projection`` reconciler; it lets the admin
device list search/filter/sort/paginate in SQL instead of aggregating and
deriving status in memory on every request.

Revision ID: 319_device_projection_table
Revises: 318_consolidated_settlement_reconciliation
Create Date: 2026-07-16
"""

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "319_device_projection_table"
down_revision = "318_consolidated_settlement_reconciliation"
branch_labels = None
depends_on = None

_TABLE = "device_projections"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE in inspector.get_table_names():
        return

    op.create_table(
        _TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("device_type", sa.String(length=20), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("serial_number", sa.String(length=120), nullable=True),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("vendor", sa.String(length=120), nullable=True),
        sa.Column("model", sa.String(length=120), nullable=True),
        sa.Column(
            "operational_status",
            sa.String(length=40),
            nullable=False,
            server_default="unknown",
        ),
        sa.Column("operational_reason", sa.String(length=160), nullable=True),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=True),
        sa.Column("subscriber_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("refreshed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "device_type", "source_id", name="uq_device_projection_source"
        ),
    )
    op.create_index("ix_device_projections_device_type", _TABLE, ["device_type"])
    op.create_index("ix_device_projections_name", _TABLE, ["name"])
    op.create_index("ix_device_projections_serial_number", _TABLE, ["serial_number"])
    op.create_index("ix_device_projections_ip_address", _TABLE, ["ip_address"])
    op.create_index("ix_device_projections_vendor", _TABLE, ["vendor"])
    op.create_index(
        "ix_device_projections_operational_status", _TABLE, ["operational_status"]
    )
    op.create_index("ix_device_projections_subscriber_id", _TABLE, ["subscriber_id"])
    op.create_index(
        "ix_device_projection_type_status",
        _TABLE,
        ["device_type", "operational_status"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return

    for index in (
        "ix_device_projection_type_status",
        "ix_device_projections_subscriber_id",
        "ix_device_projections_operational_status",
        "ix_device_projections_vendor",
        "ix_device_projections_ip_address",
        "ix_device_projections_serial_number",
        "ix_device_projections_name",
        "ix_device_projections_device_type",
    ):
        op.drop_index(index, table_name=_TABLE)
    op.drop_table(_TABLE)
