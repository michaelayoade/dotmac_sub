"""Add zabbix_host_id columns to OLT and NAS devices

Revision ID: 036_add_zabbix_host_id_columns
Revises: 035_merge_decoupling_heads
Create Date: 2026-04-18

Adds columns to track Zabbix monitoring host mapping:
- olt_devices.zabbix_host_id: Zabbix host ID for this OLT
- olt_devices.zabbix_last_sync_at: Last sync timestamp
- nas_devices.zabbix_host_id: Zabbix host ID for this NAS
- nas_devices.zabbix_last_sync_at: Last sync timestamp
"""

import sqlalchemy as sa

from alembic import op

revision = "036_add_zabbix_host_id_columns"
down_revision = "035_merge_decoupling_heads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    # Add Zabbix columns to olt_devices
    olt_columns = {col["name"] for col in inspector.get_columns("olt_devices")}

    if "zabbix_host_id" not in olt_columns:
        op.add_column(
            "olt_devices",
            sa.Column("zabbix_host_id", sa.String(20), nullable=True),
        )

    if "zabbix_last_sync_at" not in olt_columns:
        op.add_column(
            "olt_devices",
            sa.Column("zabbix_last_sync_at", sa.DateTime(timezone=True), nullable=True),
        )

    # Add Zabbix columns to nas_devices
    nas_columns = {col["name"] for col in inspector.get_columns("nas_devices")}

    if "zabbix_host_id" not in nas_columns:
        op.add_column(
            "nas_devices",
            sa.Column("zabbix_host_id", sa.String(20), nullable=True),
        )

    if "zabbix_last_sync_at" not in nas_columns:
        op.add_column(
            "nas_devices",
            sa.Column("zabbix_last_sync_at", sa.DateTime(timezone=True), nullable=True),
        )

    # Create index on zabbix_host_id for fast lookups during webhook processing
    op.create_index(
        "ix_olt_devices_zabbix_host_id",
        "olt_devices",
        ["zabbix_host_id"],
        unique=False,
        postgresql_where=sa.text("zabbix_host_id IS NOT NULL"),
    )
    op.create_index(
        "ix_nas_devices_zabbix_host_id",
        "nas_devices",
        ["zabbix_host_id"],
        unique=False,
        postgresql_where=sa.text("zabbix_host_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_nas_devices_zabbix_host_id", table_name="nas_devices")
    op.drop_index("ix_olt_devices_zabbix_host_id", table_name="olt_devices")

    op.drop_column("nas_devices", "zabbix_last_sync_at")
    op.drop_column("nas_devices", "zabbix_host_id")
    op.drop_column("olt_devices", "zabbix_last_sync_at")
    op.drop_column("olt_devices", "zabbix_host_id")
