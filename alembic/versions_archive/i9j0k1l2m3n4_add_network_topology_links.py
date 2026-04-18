"""Add network_topology_links table.

Revision ID: i9j0k1l2m3n4
Revises: i2c3d4e5f6g7
Create Date: 2026-03-22
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "i9j0k1l2m3n4"
down_revision = "i2c3d4e5f6g7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create enum types
    link_role_enum = postgresql.ENUM(
        "uplink",
        "backhaul",
        "peering",
        "lag_member",
        "crossconnect",
        "access",
        "distribution",
        "core",
        "unknown",
        name="topologylinkrole",
        create_type=False,
    )
    link_medium_enum = postgresql.ENUM(
        "fiber",
        "wireless",
        "ethernet",
        "virtual",
        "unknown",
        name="topologylinkmedium",
        create_type=False,
    )
    admin_status_enum = postgresql.ENUM(
        "enabled",
        "disabled",
        "maintenance",
        name="topologylinkadminstatus",
        create_type=False,
    )

    # Create enums if they don't exist
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    for enum_type in [link_role_enum, link_medium_enum, admin_status_enum]:
        enum_type.create(conn, checkfirst=True)

    # Create table if it doesn't exist
    if not inspector.has_table("network_topology_links"):
        op.create_table(
            "network_topology_links",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "source_device_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("network_devices.id"),
                nullable=False,
            ),
            sa.Column(
                "source_interface_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("device_interfaces.id"),
                nullable=True,
            ),
            sa.Column(
                "target_device_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("network_devices.id"),
                nullable=False,
            ),
            sa.Column(
                "target_interface_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("device_interfaces.id"),
                nullable=True,
            ),
            sa.Column("link_role", link_role_enum, server_default="unknown"),
            sa.Column("medium", link_medium_enum, server_default="unknown"),
            sa.Column("capacity_bps", sa.BigInteger, nullable=True),
            sa.Column("bundle_key", sa.String(80), nullable=True),
            sa.Column("topology_group", sa.String(80), nullable=True),
            sa.Column("admin_status", admin_status_enum, server_default="enabled"),
            sa.Column("is_active", sa.Boolean, server_default="true"),
            sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("confirmed_by", sa.String(120), nullable=True),
            sa.Column("notes", sa.Text, nullable=True),
            sa.Column("metadata", postgresql.JSON, nullable=True),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()
            ),
        )

        # Unique constraint
        op.create_unique_constraint(
            "uq_topology_link_endpoints",
            "network_topology_links",
            [
                "source_device_id",
                "source_interface_id",
                "target_device_id",
                "target_interface_id",
            ],
        )

        # Indexes
        op.create_index(
            "ix_topology_link_source_device",
            "network_topology_links",
            ["source_device_id"],
        )
        op.create_index(
            "ix_topology_link_target_device",
            "network_topology_links",
            ["target_device_id"],
        )
        op.create_index(
            "ix_topology_link_source_iface",
            "network_topology_links",
            ["source_interface_id"],
        )
        op.create_index(
            "ix_topology_link_target_iface",
            "network_topology_links",
            ["target_interface_id"],
        )
        op.create_index(
            "ix_topology_link_bundle", "network_topology_links", ["bundle_key"]
        )
        op.create_index(
            "ix_topology_link_group", "network_topology_links", ["topology_group"]
        )


def downgrade() -> None:
    op.drop_table("network_topology_links")
