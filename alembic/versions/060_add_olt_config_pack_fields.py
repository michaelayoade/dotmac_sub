"""Add OLT Config Pack fields for centralized ONT provisioning defaults

Revision ID: 060_add_olt_config_pack_fields
Revises: 059_add_olt_rest_and_rate_limit
Create Date: 2026-04-24

The OLT Config Pack provides default values for ONT authorization and provisioning:
- Authorization profile IDs (line/service profiles)
- TR-069 binding profile ID
- VLAN assignments by purpose (internet, management, TR-069, VoIP, IPTV)
- Provisioning knobs (ip-index, wan-config profile)
- Connection request credentials for ACS

ONTs inherit these defaults from their OLT, reducing per-ONT configuration.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "060_add_olt_config_pack_fields"
down_revision = "059_add_olt_rest_and_rate_limit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_columns = [col["name"] for col in inspector.get_columns("olt_devices")]

    # Authorization profiles (OLT-local IDs)
    if "default_line_profile_id" not in existing_columns:
        op.add_column(
            "olt_devices",
            sa.Column(
                "default_line_profile_id",
                sa.Integer(),
                nullable=True,
                comment="OLT-local ont-lineprofile profile-id for authorization",
            ),
        )

    if "default_service_profile_id" not in existing_columns:
        op.add_column(
            "olt_devices",
            sa.Column(
                "default_service_profile_id",
                sa.Integer(),
                nullable=True,
                comment="OLT-local ont-srvprofile profile-id for authorization",
            ),
        )

    # TR-069 binding profile
    if "default_tr069_olt_profile_id" not in existing_columns:
        op.add_column(
            "olt_devices",
            sa.Column(
                "default_tr069_olt_profile_id",
                sa.Integer(),
                nullable=True,
                comment="OLT-local TR-069 server profile ID for ACS binding",
            ),
        )

    # VLAN assignments (FKs to vlans table)
    if "internet_vlan_id" not in existing_columns:
        op.add_column(
            "olt_devices",
            sa.Column(
                "internet_vlan_id",
                UUID(as_uuid=True),
                nullable=True,
                comment="Default internet/data VLAN for ONTs",
            ),
        )
        op.create_foreign_key(
            "fk_olt_devices_internet_vlan",
            "olt_devices",
            "vlans",
            ["internet_vlan_id"],
            ["id"],
            ondelete="SET NULL",
        )

    if "management_vlan_id" not in existing_columns:
        op.add_column(
            "olt_devices",
            sa.Column(
                "management_vlan_id",
                UUID(as_uuid=True),
                nullable=True,
                comment="Default management VLAN for ONT IPHOST",
            ),
        )
        op.create_foreign_key(
            "fk_olt_devices_management_vlan",
            "olt_devices",
            "vlans",
            ["management_vlan_id"],
            ["id"],
            ondelete="SET NULL",
        )

    if "tr069_vlan_id" not in existing_columns:
        op.add_column(
            "olt_devices",
            sa.Column(
                "tr069_vlan_id",
                UUID(as_uuid=True),
                nullable=True,
                comment="VLAN for TR-069/ACS traffic (often same as management)",
            ),
        )
        op.create_foreign_key(
            "fk_olt_devices_tr069_vlan",
            "olt_devices",
            "vlans",
            ["tr069_vlan_id"],
            ["id"],
            ondelete="SET NULL",
        )

    if "voip_vlan_id" not in existing_columns:
        op.add_column(
            "olt_devices",
            sa.Column(
                "voip_vlan_id",
                UUID(as_uuid=True),
                nullable=True,
                comment="Default VoIP VLAN (optional)",
            ),
        )
        op.create_foreign_key(
            "fk_olt_devices_voip_vlan",
            "olt_devices",
            "vlans",
            ["voip_vlan_id"],
            ["id"],
            ondelete="SET NULL",
        )

    if "iptv_vlan_id" not in existing_columns:
        op.add_column(
            "olt_devices",
            sa.Column(
                "iptv_vlan_id",
                UUID(as_uuid=True),
                nullable=True,
                comment="Default IPTV/multicast VLAN (optional)",
            ),
        )
        op.create_foreign_key(
            "fk_olt_devices_iptv_vlan",
            "olt_devices",
            "vlans",
            ["iptv_vlan_id"],
            ["id"],
            ondelete="SET NULL",
        )

    # OLT-side provisioning knobs
    if "default_internet_config_ip_index" not in existing_columns:
        op.add_column(
            "olt_devices",
            sa.Column(
                "default_internet_config_ip_index",
                sa.Integer(),
                nullable=True,
                server_default="0",
                comment="ip-index for ont internet-config command (activates TCP stack)",
            ),
        )

    if "default_wan_config_profile_id" not in existing_columns:
        op.add_column(
            "olt_devices",
            sa.Column(
                "default_wan_config_profile_id",
                sa.Integer(),
                nullable=True,
                server_default="0",
                comment="profile-id for ont wan-config command (sets route+NAT mode)",
            ),
        )

    # TR-069 connection request credentials
    if "default_cr_username" not in existing_columns:
        op.add_column(
            "olt_devices",
            sa.Column(
                "default_cr_username",
                sa.String(120),
                nullable=True,
                comment="Default connection request username for ACS on-demand management",
            ),
        )

    if "default_cr_password" not in existing_columns:
        op.add_column(
            "olt_devices",
            sa.Column(
                "default_cr_password",
                sa.String(512),
                nullable=True,
                comment="Default connection request password (encrypted at rest)",
            ),
        )

    # -------------------------------------------------------------------------
    # Data migration: populate config pack from existing provisioning profiles
    # -------------------------------------------------------------------------
    _populate_olt_config_pack_from_profiles(conn)


def _populate_olt_config_pack_from_profiles(conn) -> None:
    """Populate OLT config pack fields from existing OLT-scoped provisioning profiles."""
    # Get all OLTs
    olts = conn.execute(
        sa.text("SELECT id, name FROM olt_devices WHERE is_active = true")
    ).fetchall()

    for olt_id, olt_name in olts:
        # Find the best provisioning profile for this OLT
        profile = conn.execute(
            sa.text("""
                SELECT
                    authorization_line_profile_id,
                    authorization_service_profile_id,
                    mgmt_vlan_tag
                FROM ont_provisioning_profiles
                WHERE olt_device_id = :olt_id
                  AND is_active = true
                ORDER BY is_default DESC, updated_at DESC
                LIMIT 1
            """),
            {"olt_id": olt_id},
        ).fetchone()

        updates = {}

        if profile:
            line_id, service_id, mgmt_vlan_tag = profile
            if line_id is not None:
                updates["default_line_profile_id"] = line_id
            if service_id is not None:
                updates["default_service_profile_id"] = service_id

        # Find VLANs by purpose for this OLT
        vlans = conn.execute(
            sa.text("""
                SELECT id, purpose FROM vlans
                WHERE olt_device_id = :olt_id AND is_active = true
            """),
            {"olt_id": olt_id},
        ).fetchall()

        for vlan_id, purpose in vlans:
            if purpose == "internet" and "internet_vlan_id" not in updates:
                updates["internet_vlan_id"] = vlan_id
            elif purpose == "management" and "management_vlan_id" not in updates:
                updates["management_vlan_id"] = vlan_id
                # TR-069 often uses the same as management
                if "tr069_vlan_id" not in updates:
                    updates["tr069_vlan_id"] = vlan_id
            elif purpose == "tr069" and "tr069_vlan_id" not in updates:
                updates["tr069_vlan_id"] = vlan_id
            elif purpose == "voip" and "voip_vlan_id" not in updates:
                updates["voip_vlan_id"] = vlan_id
            elif purpose == "iptv" and "iptv_vlan_id" not in updates:
                updates["iptv_vlan_id"] = vlan_id

        if updates:
            set_clause = ", ".join(f"{k} = :{k}" for k in updates)
            updates["olt_id"] = olt_id
            conn.execute(
                sa.text(f"UPDATE olt_devices SET {set_clause} WHERE id = :olt_id"),
                updates,
            )
            print(f"  Updated OLT '{olt_name}' config pack: {list(updates.keys())}")


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_columns = [col["name"] for col in inspector.get_columns("olt_devices")]

    # Drop foreign keys first
    fks_to_drop = [
        "fk_olt_devices_internet_vlan",
        "fk_olt_devices_management_vlan",
        "fk_olt_devices_tr069_vlan",
        "fk_olt_devices_voip_vlan",
        "fk_olt_devices_iptv_vlan",
    ]
    existing_fks = [fk["name"] for fk in inspector.get_foreign_keys("olt_devices")]
    for fk in fks_to_drop:
        if fk in existing_fks:
            op.drop_constraint(fk, "olt_devices", type_="foreignkey")

    # Drop columns
    columns_to_drop = [
        "default_line_profile_id",
        "default_service_profile_id",
        "default_tr069_olt_profile_id",
        "internet_vlan_id",
        "management_vlan_id",
        "tr069_vlan_id",
        "voip_vlan_id",
        "iptv_vlan_id",
        "default_internet_config_ip_index",
        "default_wan_config_profile_id",
        "default_cr_username",
        "default_cr_password",
    ]

    for col in columns_to_drop:
        if col in existing_columns:
            op.drop_column("olt_devices", col)
