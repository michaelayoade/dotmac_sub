"""Add ONT provisioning profile and WAN service tables.

Revision ID: d2e5f7a9b1c3
Revises: c1f4d6a8b9e2
Create Date: 2026-03-10
"""

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import ENUM as PGEnum
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "d2e5f7a9b1c3"
down_revision = "c1f4d6a8b9e2"
branch_labels = None
depends_on = None


def _create_enum_if_not_exists(name: str, values: list[str]) -> None:
    """Create a PostgreSQL enum type if it does not already exist."""
    conn = op.get_bind()
    result = conn.execute(
        sa.text("SELECT 1 FROM pg_type WHERE typname = :name"),
        {"name": name},
    )
    if result.fetchone() is None:
        sa.Enum(*values, name=name).create(conn)


def upgrade() -> None:
    conn = op.get_bind()
    inspector = inspect(conn)

    # Create enum types that don't exist yet
    _create_enum_if_not_exists(
        "ontprofiletype", ["residential", "business", "management"]
    )
    _create_enum_if_not_exists(
        "wanservicetype", ["internet", "iptv", "voip", "management", "data"]
    )
    _create_enum_if_not_exists(
        "vlanmode", ["tagged", "untagged", "transparent", "translate"]
    )
    _create_enum_if_not_exists(
        "wanconnectiontype", ["pppoe", "dhcp", "static", "bridged"]
    )
    _create_enum_if_not_exists(
        "pppoepasswordmode", ["from_credential", "generate", "static"]
    )
    _create_enum_if_not_exists("configmethod", ["omci", "tr069"])
    _create_enum_if_not_exists("onumode", ["routing", "bridging"])
    _create_enum_if_not_exists("ipprotocol", ["ipv4", "dual_stack"])
    _create_enum_if_not_exists("mgmtipmode", ["inactive", "static_ip", "dhcp"])
    _create_enum_if_not_exists(
        "ontprovisioningstatus",
        ["unprovisioned", "provisioned", "drift_detected", "failed"],
    )

    # Create ont_provisioning_profiles table
    if not inspector.has_table("ont_provisioning_profiles"):
        op.create_table(
            "ont_provisioning_profiles",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "organization_id",
                UUID(as_uuid=True),
                sa.ForeignKey("organizations.id"),
                nullable=False,
            ),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column(
                "profile_type",
                PGEnum(
                    "residential",
                    "business",
                    "management",
                    name="ontprofiletype",
                    create_constraint=False,
                    create_type=False,
                ),
                nullable=False,
                server_default="residential",
            ),
            sa.Column("description", sa.Text),
            # Device-level defaults
            sa.Column(
                "config_method",
                PGEnum(
                    "omci",
                    "tr069",
                    name="configmethod",
                    create_constraint=False,
                    create_type=False,
                ),
            ),
            sa.Column(
                "onu_mode",
                PGEnum(
                    "routing",
                    "bridging",
                    name="onumode",
                    create_constraint=False,
                    create_type=False,
                ),
            ),
            sa.Column(
                "ip_protocol",
                PGEnum(
                    "ipv4",
                    "dual_stack",
                    name="ipprotocol",
                    create_constraint=False,
                    create_type=False,
                ),
            ),
            # Speed profiles
            sa.Column(
                "download_speed_profile_id",
                UUID(as_uuid=True),
                sa.ForeignKey("speed_profiles.id"),
            ),
            sa.Column(
                "upload_speed_profile_id",
                UUID(as_uuid=True),
                sa.ForeignKey("speed_profiles.id"),
            ),
            # Management plane
            sa.Column(
                "mgmt_ip_mode",
                PGEnum(
                    "inactive",
                    "static_ip",
                    "dhcp",
                    name="mgmtipmode",
                    create_constraint=False,
                    create_type=False,
                ),
            ),
            sa.Column("mgmt_vlan_tag", sa.Integer),
            sa.Column("mgmt_remote_access", sa.Boolean, server_default="false"),
            # WiFi
            sa.Column("wifi_enabled", sa.Boolean, server_default="true"),
            sa.Column("wifi_ssid_template", sa.String(120)),
            sa.Column("wifi_security_mode", sa.String(40)),
            sa.Column("wifi_channel", sa.String(10)),
            sa.Column("wifi_band", sa.String(20)),
            # VoIP
            sa.Column("voip_enabled", sa.Boolean, server_default="false"),
            # Metadata
            sa.Column("is_default", sa.Boolean, server_default="false"),
            sa.Column("is_active", sa.Boolean, server_default="true"),
            sa.Column("notes", sa.Text),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
            ),
            sa.UniqueConstraint(
                "organization_id", "name", name="uq_ont_prov_profiles_org_name"
            ),
        )

    # Create ont_profile_wan_services table
    if not inspector.has_table("ont_profile_wan_services"):
        op.create_table(
            "ont_profile_wan_services",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "profile_id",
                UUID(as_uuid=True),
                sa.ForeignKey("ont_provisioning_profiles.id", ondelete="CASCADE"),
                nullable=False,
            ),
            # Service identity
            sa.Column(
                "service_type",
                PGEnum(
                    "internet",
                    "iptv",
                    "voip",
                    "management",
                    "data",
                    name="wanservicetype",
                    create_constraint=False,
                    create_type=False,
                ),
                nullable=False,
                server_default="internet",
            ),
            sa.Column("name", sa.String(120)),
            sa.Column("priority", sa.Integer, server_default="1"),
            # L2: VLAN
            sa.Column(
                "vlan_mode",
                PGEnum(
                    "tagged",
                    "untagged",
                    "transparent",
                    "translate",
                    name="vlanmode",
                    create_constraint=False,
                    create_type=False,
                ),
                nullable=False,
                server_default="tagged",
            ),
            sa.Column("s_vlan", sa.Integer),
            sa.Column("c_vlan", sa.Integer),
            sa.Column("cos_priority", sa.Integer),
            sa.Column("mtu", sa.Integer, server_default="1500"),
            # L3: Connection
            sa.Column(
                "connection_type",
                PGEnum(
                    "pppoe",
                    "dhcp",
                    "static",
                    "bridged",
                    name="wanconnectiontype",
                    create_constraint=False,
                    create_type=False,
                ),
                nullable=False,
                server_default="pppoe",
            ),
            sa.Column("nat_enabled", sa.Boolean, server_default="true"),
            sa.Column(
                "ip_mode",
                PGEnum(
                    "ipv4",
                    "dual_stack",
                    name="ipprotocol",
                    create_constraint=False,
                    create_type=False,
                ),
            ),
            # PPPoE
            sa.Column("pppoe_username_template", sa.String(200)),
            sa.Column(
                "pppoe_password_mode",
                PGEnum(
                    "from_credential",
                    "generate",
                    "static",
                    name="pppoepasswordmode",
                    create_constraint=False,
                    create_type=False,
                ),
            ),
            sa.Column("pppoe_static_password", sa.String(500)),
            # Static IP
            sa.Column("static_ip_source", sa.String(200)),
            # LAN port binding
            sa.Column("bind_lan_ports", sa.JSON),
            sa.Column("bind_ssid_index", sa.Integer),
            # OMCI-specific
            sa.Column("gem_port_id", sa.Integer),
            sa.Column("t_cont_profile", sa.String(120)),
            # Metadata
            sa.Column("is_active", sa.Boolean, server_default="true"),
            sa.Column("notes", sa.Text),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
            ),
        )


def downgrade() -> None:
    op.drop_table("ont_profile_wan_services")
    op.drop_table("ont_provisioning_profiles")
