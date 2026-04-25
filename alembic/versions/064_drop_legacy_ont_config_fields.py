"""Drop legacy ONT desired-state config fields

Revision ID: 064_drop_legacy_ont_config_fields
Revises: 063_simplify_bundle_assignment_status
Create Date: 2026-04-24

After migrating ONTs to bundle assignments with sparse overrides, the legacy
desired-state columns on ont_units are no longer used. This migration drops:

WAN Configuration:
- wan_vlan_id (UUID FK to vlans)
- wan_mode (enum)
- config_method (enum)
- ip_protocol (enum)
- pppoe_username (string)
- pppoe_password (string, encrypted)

WiFi Configuration:
- wifi_ssid (string)
- wifi_password (string, encrypted)

Management IP Configuration:
- mgmt_ip_mode (enum)
- mgmt_vlan_id (UUID FK to vlans)
- mgmt_ip_address (string)

Legacy Profile Link:
- provisioning_profile_id (UUID FK to ont_provisioning_profiles)

IMPORTANT: Run the backfill script before this migration to migrate all
passwords and config to OntConfigOverride:

    python scripts/backfill_ont_bundle_assignments.py --apply
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "064_drop_legacy_ont_config_fields"
down_revision = "063_simplify_bundle_assignment_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    ont_columns = [col["name"] for col in inspector.get_columns("ont_units")]

    # Drop foreign key constraints first
    fk_constraints = inspector.get_foreign_keys("ont_units")
    fk_names = {fk["name"] for fk in fk_constraints if fk["name"]}

    # wan_vlan_id FK
    for fk in fk_constraints:
        if "wan_vlan_id" in fk.get("constrained_columns", []):
            if fk.get("name"):
                op.drop_constraint(fk["name"], "ont_units", type_="foreignkey")
                break

    # mgmt_vlan_id FK
    for fk in fk_constraints:
        if "mgmt_vlan_id" in fk.get("constrained_columns", []):
            if fk.get("name"):
                op.drop_constraint(fk["name"], "ont_units", type_="foreignkey")
                break

    # provisioning_profile_id FK
    for fk in fk_constraints:
        if "provisioning_profile_id" in fk.get("constrained_columns", []):
            if fk.get("name"):
                op.drop_constraint(fk["name"], "ont_units", type_="foreignkey")
                break

    # Drop columns
    columns_to_drop = [
        # WAN configuration
        "wan_vlan_id",
        "wan_mode",
        "config_method",
        "ip_protocol",
        "pppoe_username",
        "pppoe_password",
        # WiFi configuration
        "wifi_ssid",
        "wifi_password",
        # Management IP configuration
        "mgmt_ip_mode",
        "mgmt_vlan_id",
        "mgmt_ip_address",
        # Legacy profile link
        "provisioning_profile_id",
    ]

    for col in columns_to_drop:
        if col in ont_columns:
            op.drop_column("ont_units", col)


def downgrade() -> None:
    # Re-add columns (data is lost - this is a destructive migration)
    # WAN configuration
    op.add_column(
        "ont_units",
        sa.Column("wan_vlan_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "ont_units",
        sa.Column(
            "wan_mode",
            sa.Enum("dhcp", "static", "pppoe", name="wanmode"),
            nullable=True,
        ),
    )
    op.add_column(
        "ont_units",
        sa.Column(
            "config_method",
            sa.Enum("omci", "tr069", name="configmethod"),
            nullable=True,
        ),
    )
    op.add_column(
        "ont_units",
        sa.Column(
            "ip_protocol",
            sa.Enum("ipv4", "ipv6", "dual_stack", name="ipprotocol"),
            nullable=True,
        ),
    )
    op.add_column(
        "ont_units",
        sa.Column("pppoe_username", sa.String(120), nullable=True),
    )
    op.add_column(
        "ont_units",
        sa.Column("pppoe_password", sa.String(512), nullable=True),
    )

    # WiFi configuration
    op.add_column(
        "ont_units",
        sa.Column("wifi_ssid", sa.String(64), nullable=True),
    )
    op.add_column(
        "ont_units",
        sa.Column("wifi_password", sa.String(512), nullable=True),
    )

    # Management IP configuration
    op.add_column(
        "ont_units",
        sa.Column(
            "mgmt_ip_mode",
            sa.Enum("static", "dhcp", name="mgmtipmode"),
            nullable=True,
        ),
    )
    op.add_column(
        "ont_units",
        sa.Column("mgmt_vlan_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "ont_units",
        sa.Column("mgmt_ip_address", sa.String(64), nullable=True),
    )

    # Legacy profile link
    op.add_column(
        "ont_units",
        sa.Column(
            "provisioning_profile_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
    )

    # Re-add foreign keys
    op.create_foreign_key(
        "ont_units_wan_vlan_id_fkey",
        "ont_units",
        "vlans",
        ["wan_vlan_id"],
        ["id"],
    )
    op.create_foreign_key(
        "ont_units_mgmt_vlan_id_fkey",
        "ont_units",
        "vlans",
        ["mgmt_vlan_id"],
        ["id"],
    )
    op.create_foreign_key(
        "ont_units_provisioning_profile_id_fkey",
        "ont_units",
        "ont_provisioning_profiles",
        ["provisioning_profile_id"],
        ["id"],
    )
