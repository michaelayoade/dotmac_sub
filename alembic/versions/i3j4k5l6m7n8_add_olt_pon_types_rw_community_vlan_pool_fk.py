"""Add supported_pon_types, snmp_rw_community, VLAN/IP pool OLT FK.

Revision ID: i3j4k5l6m7n8
Revises: h2i3j4k5l6m7
Create Date: 2026-03-16
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "i3j4k5l6m7n8"
down_revision = ("h2i3j4k5l6m7", "o3p4q5r6s7t8")
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    # ---- olt_devices.supported_pon_types ----
    if inspector.has_table("olt_devices"):
        cols = {col["name"] for col in inspector.get_columns("olt_devices")}
        if "supported_pon_types" not in cols:
            op.add_column(
                "olt_devices",
                sa.Column("supported_pon_types", sa.String(120), nullable=True),
            )

    # ---- network_devices.snmp_rw_community ----
    if inspector.has_table("network_devices"):
        cols = {col["name"] for col in inspector.get_columns("network_devices")}
        if "snmp_rw_community" not in cols:
            op.add_column(
                "network_devices",
                sa.Column("snmp_rw_community", sa.String(255), nullable=True),
            )

    # ---- vlans.olt_device_id FK ----
    if inspector.has_table("vlans"):
        cols = {col["name"] for col in inspector.get_columns("vlans")}
        if "olt_device_id" not in cols:
            op.add_column(
                "vlans",
                sa.Column(
                    "olt_device_id",
                    UUID(as_uuid=True),
                    sa.ForeignKey("olt_devices.id", ondelete="SET NULL"),
                    nullable=True,
                ),
            )
            op.create_index("ix_vlans_olt_device_id", "vlans", ["olt_device_id"])

    # ---- ip_pools.olt_device_id FK ----
    if inspector.has_table("ip_pools"):
        cols = {col["name"] for col in inspector.get_columns("ip_pools")}
        if "olt_device_id" not in cols:
            op.add_column(
                "ip_pools",
                sa.Column(
                    "olt_device_id",
                    UUID(as_uuid=True),
                    sa.ForeignKey("olt_devices.id", ondelete="SET NULL"),
                    nullable=True,
                ),
            )
            op.create_index("ix_ip_pools_olt_device_id", "ip_pools", ["olt_device_id"])


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if inspector.has_table("ip_pools"):
        cols = {col["name"] for col in inspector.get_columns("ip_pools")}
        if "olt_device_id" in cols:
            op.drop_index("ix_ip_pools_olt_device_id", table_name="ip_pools")
            op.drop_column("ip_pools", "olt_device_id")

    if inspector.has_table("vlans"):
        cols = {col["name"] for col in inspector.get_columns("vlans")}
        if "olt_device_id" in cols:
            op.drop_index("ix_vlans_olt_device_id", table_name="vlans")
            op.drop_column("vlans", "olt_device_id")

    if inspector.has_table("network_devices"):
        cols = {col["name"] for col in inspector.get_columns("network_devices")}
        if "snmp_rw_community" in cols:
            op.drop_column("network_devices", "snmp_rw_community")

    if inspector.has_table("olt_devices"):
        cols = {col["name"] for col in inspector.get_columns("olt_devices")}
        if "supported_pon_types" in cols:
            op.drop_column("olt_devices", "supported_pon_types")
