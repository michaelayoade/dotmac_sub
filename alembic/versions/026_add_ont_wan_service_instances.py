"""Add ont_wan_service_instances table for multi-WAN support.

This table stores per-ONT WAN service instances with resolved credentials
and VLANs, bridging profile templates to actual device configuration.

Revision ID: 026_add_ont_wan_service_instances
Revises: 025_acs_interval_3600
Create Date: 2026-04-17

"""

import sqlalchemy as sa

from alembic import op

revision = "026_add_ont_wan_service_instances"
down_revision = "025_acs_interval_3600"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Check if table already exists (idempotent)
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_name = 'ont_wan_service_instances'"
        )
    )
    if result.fetchone():
        return  # Table already exists, nothing to do

    # Create the wanserviceprovisioningstatus enum type if it doesn't exist
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM pg_type WHERE typname = 'wanserviceprovisioningstatus'"
        )
    )
    if not result.fetchone():
        op.execute(
            "CREATE TYPE wanserviceprovisioningstatus AS ENUM "
            "('pending', 'provisioned', 'failed')"
        )

    # Create the table using raw SQL to avoid enum creation conflicts
    # The wanservicetype, vlanmode, and wanconnectiontype enums already exist
    op.execute("""
        CREATE TABLE ont_wan_service_instances (
            id UUID NOT NULL,
            ont_id UUID NOT NULL,
            source_profile_service_id UUID,
            service_type wanservicetype NOT NULL DEFAULT 'internet',
            name VARCHAR(120),
            priority INTEGER NOT NULL DEFAULT 1,
            is_active BOOLEAN NOT NULL DEFAULT true,
            vlan_mode vlanmode NOT NULL DEFAULT 'tagged',
            vlan_id UUID,
            s_vlan INTEGER,
            c_vlan INTEGER,
            connection_type wanconnectiontype NOT NULL DEFAULT 'pppoe',
            nat_enabled BOOLEAN NOT NULL DEFAULT true,
            pppoe_username VARCHAR(200),
            pppoe_password VARCHAR(500),
            static_ip VARCHAR(64),
            static_gateway VARCHAR(64),
            static_dns VARCHAR(200),
            provisioning_status wanserviceprovisioningstatus NOT NULL DEFAULT 'pending',
            last_provisioned_at TIMESTAMP WITH TIME ZONE,
            last_error VARCHAR(500),
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            PRIMARY KEY (id),
            CONSTRAINT fk_ont_wan_service_instances_ont_id
                FOREIGN KEY (ont_id) REFERENCES ont_units(id) ON DELETE CASCADE,
            CONSTRAINT fk_ont_wan_service_instances_source_profile_service_id
                FOREIGN KEY (source_profile_service_id) REFERENCES ont_profile_wan_services(id) ON DELETE SET NULL,
            CONSTRAINT fk_ont_wan_service_instances_vlan_id
                FOREIGN KEY (vlan_id) REFERENCES vlans(id) ON DELETE SET NULL
        )
    """)

    # Create indexes
    op.create_index(
        "ix_ont_wan_service_instances_ont_id",
        "ont_wan_service_instances",
        ["ont_id"],
    )
    op.create_index(
        "ix_ont_wan_service_instances_ont_type",
        "ont_wan_service_instances",
        ["ont_id", "service_type"],
    )


def downgrade() -> None:
    # Check if table exists before dropping
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_name = 'ont_wan_service_instances'"
        )
    )
    if not result.fetchone():
        return  # Table doesn't exist, nothing to do

    # Drop indexes (if they exist)
    op.execute(
        "DROP INDEX IF EXISTS ix_ont_wan_service_instances_ont_type"
    )
    op.execute(
        "DROP INDEX IF EXISTS ix_ont_wan_service_instances_ont_id"
    )
    op.drop_table("ont_wan_service_instances")

    # Only drop the enum if no other tables use it
    # (wanserviceprovisioningstatus is only used by this table, so we can drop it)
    op.execute("DROP TYPE IF EXISTS wanserviceprovisioningstatus")
