"""Add ACS Config Pack fields to OnuType and mgmt_ip_pool_id to OLTDevice

Revision ID: 061_add_acs_config_pack_and_mgmt_ip_pool
Revises: 060_add_olt_config_pack_fields
Create Date: 2026-04-24

ACS Config Pack on OnuType provides device-model-specific TR-069 configuration:
- TR-069 parameter paths for WiFi, WAN, etc.
- Default WiFi settings per model
- Config method preference (TR-069 vs OMCI)

Management IP Pool on OLTDevice enables auto-allocation of management IPs
to ONTs during authorization.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "061_add_acs_config_pack_and_mgmt_ip_pool"
down_revision = "060_add_olt_config_pack_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    # -------------------------------------------------------------------------
    # OnuType: ACS Config Pack fields
    # -------------------------------------------------------------------------
    onu_type_columns = [col["name"] for col in inspector.get_columns("onu_types")]

    # TR-069 data model and config method
    if "tr069_data_model" not in onu_type_columns:
        op.add_column(
            "onu_types",
            sa.Column(
                "tr069_data_model",
                sa.String(20),
                nullable=True,
                comment="TR-069 data model: 'tr181' or 'tr098'",
            ),
        )

    if "config_method_preference" not in onu_type_columns:
        op.add_column(
            "onu_types",
            sa.Column(
                "config_method_preference",
                sa.String(20),
                nullable=True,
                comment="Preferred config method: 'tr069', 'omci', or 'both'",
            ),
        )

    # WiFi TR-069 parameter paths
    wifi_path_columns = [
        ("wifi_ssid_path", "TR-069 path for WiFi SSID"),
        ("wifi_password_path", "TR-069 path for WiFi password/key"),
        ("wifi_enabled_path", "TR-069 path for WiFi enable/disable"),
        ("wifi_channel_path", "TR-069 path for WiFi channel"),
        ("wifi_security_mode_path", "TR-069 path for WiFi security mode"),
    ]
    for col_name, comment in wifi_path_columns:
        if col_name not in onu_type_columns:
            op.add_column(
                "onu_types",
                sa.Column(col_name, sa.String(255), nullable=True, comment=comment),
            )

    # WAN TR-069 parameter paths
    wan_path_columns = [
        ("wan_pppoe_username_path", "TR-069 path for PPPoE username"),
        ("wan_pppoe_password_path", "TR-069 path for PPPoE password"),
        ("wan_connection_type_path", "TR-069 path for WAN connection type"),
    ]
    for col_name, comment in wan_path_columns:
        if col_name not in onu_type_columns:
            op.add_column(
                "onu_types",
                sa.Column(col_name, sa.String(255), nullable=True, comment=comment),
            )

    # Default WiFi settings
    if "default_wifi_security_mode" not in onu_type_columns:
        op.add_column(
            "onu_types",
            sa.Column(
                "default_wifi_security_mode",
                sa.String(50),
                nullable=True,
                server_default="WPA2-Personal",
                comment="Default WiFi security mode",
            ),
        )

    if "default_wifi_channel" not in onu_type_columns:
        op.add_column(
            "onu_types",
            sa.Column(
                "default_wifi_channel",
                sa.String(10),
                nullable=True,
                server_default="auto",
                comment="Default WiFi channel",
            ),
        )

    # Firmware baseline
    if "min_firmware_version" not in onu_type_columns:
        op.add_column(
            "onu_types",
            sa.Column(
                "min_firmware_version",
                sa.String(50),
                nullable=True,
                comment="Minimum firmware version for full feature support",
            ),
        )

    # -------------------------------------------------------------------------
    # OLTDevice: Management IP Pool
    # -------------------------------------------------------------------------
    olt_columns = [col["name"] for col in inspector.get_columns("olt_devices")]

    if "mgmt_ip_pool_id" not in olt_columns:
        op.add_column(
            "olt_devices",
            sa.Column(
                "mgmt_ip_pool_id",
                UUID(as_uuid=True),
                nullable=True,
                comment="IP pool for auto-allocating management IPs to ONTs",
            ),
        )
        op.create_foreign_key(
            "fk_olt_devices_mgmt_ip_pool",
            "olt_devices",
            "ip_pools",
            ["mgmt_ip_pool_id"],
            ["id"],
            ondelete="SET NULL",
        )

    # -------------------------------------------------------------------------
    # IPv4Address: Management IP tracking
    # -------------------------------------------------------------------------
    ipv4_columns = [col["name"] for col in inspector.get_columns("ipv4_addresses")]

    if "ont_unit_id" not in ipv4_columns:
        op.add_column(
            "ipv4_addresses",
            sa.Column(
                "ont_unit_id",
                UUID(as_uuid=True),
                nullable=True,
                comment="ONT with this IP as management address",
            ),
        )
        op.create_foreign_key(
            "fk_ipv4_addresses_ont_unit",
            "ipv4_addresses",
            "ont_units",
            ["ont_unit_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_index(
            "ix_ipv4_addresses_ont_unit_id",
            "ipv4_addresses",
            ["ont_unit_id"],
        )

    if "allocation_type" not in ipv4_columns:
        op.add_column(
            "ipv4_addresses",
            sa.Column(
                "allocation_type",
                sa.String(20),
                nullable=True,
                comment="Type of allocation: management, wan, static",
            ),
        )

    # -------------------------------------------------------------------------
    # Seed common Huawei TR-181 paths for existing ONU types
    # -------------------------------------------------------------------------
    _seed_huawei_tr181_paths(conn)


def _seed_huawei_tr181_paths(conn) -> None:
    """Populate TR-069 paths for Huawei devices using TR-181 data model."""
    # Common TR-181 paths for Huawei HG8xxx series
    huawei_tr181_paths = {
        "tr069_data_model": "tr181",
        "config_method_preference": "tr069",
        "wifi_ssid_path": "Device.WiFi.SSID.1.SSID",
        "wifi_password_path": "Device.WiFi.AccessPoint.1.Security.KeyPassphrase",
        "wifi_enabled_path": "Device.WiFi.SSID.1.Enable",
        "wifi_channel_path": "Device.WiFi.Radio.1.Channel",
        "wifi_security_mode_path": "Device.WiFi.AccessPoint.1.Security.ModeEnabled",
        "wan_pppoe_username_path": "Device.PPP.Interface.1.Username",
        "wan_pppoe_password_path": "Device.PPP.Interface.1.Password",
        "wan_connection_type_path": "Device.IP.Interface.1.Type",
    }

    # Update Huawei ONU types (name contains 'Huawei' or 'HG8')
    result = conn.execute(
        sa.text("""
            UPDATE onu_types
            SET tr069_data_model = :tr069_data_model,
                config_method_preference = :config_method_preference,
                wifi_ssid_path = :wifi_ssid_path,
                wifi_password_path = :wifi_password_path,
                wifi_enabled_path = :wifi_enabled_path,
                wifi_channel_path = :wifi_channel_path,
                wifi_security_mode_path = :wifi_security_mode_path,
                wan_pppoe_username_path = :wan_pppoe_username_path,
                wan_pppoe_password_path = :wan_pppoe_password_path,
                wan_connection_type_path = :wan_connection_type_path
            WHERE (name ILIKE '%Huawei%' OR name ILIKE '%HG8%')
              AND tr069_data_model IS NULL
        """),
        huawei_tr181_paths,
    )
    if result.rowcount > 0:
        print(f"  Updated {result.rowcount} Huawei ONU types with TR-181 paths")


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    # Drop IPv4Address management IP tracking columns
    ipv4_fks = [fk["name"] for fk in inspector.get_foreign_keys("ipv4_addresses")]
    if "fk_ipv4_addresses_ont_unit" in ipv4_fks:
        op.drop_constraint("fk_ipv4_addresses_ont_unit", "ipv4_addresses", type_="foreignkey")

    ipv4_indexes = [idx["name"] for idx in inspector.get_indexes("ipv4_addresses")]
    if "ix_ipv4_addresses_ont_unit_id" in ipv4_indexes:
        op.drop_index("ix_ipv4_addresses_ont_unit_id", "ipv4_addresses")

    ipv4_columns = [col["name"] for col in inspector.get_columns("ipv4_addresses")]
    if "ont_unit_id" in ipv4_columns:
        op.drop_column("ipv4_addresses", "ont_unit_id")
    if "allocation_type" in ipv4_columns:
        op.drop_column("ipv4_addresses", "allocation_type")

    # Drop OLTDevice FK and column
    olt_fks = [fk["name"] for fk in inspector.get_foreign_keys("olt_devices")]
    if "fk_olt_devices_mgmt_ip_pool" in olt_fks:
        op.drop_constraint("fk_olt_devices_mgmt_ip_pool", "olt_devices", type_="foreignkey")

    olt_columns = [col["name"] for col in inspector.get_columns("olt_devices")]
    if "mgmt_ip_pool_id" in olt_columns:
        op.drop_column("olt_devices", "mgmt_ip_pool_id")

    # Drop OnuType ACS Config Pack columns
    onu_type_columns = [col["name"] for col in inspector.get_columns("onu_types")]
    columns_to_drop = [
        "tr069_data_model",
        "config_method_preference",
        "wifi_ssid_path",
        "wifi_password_path",
        "wifi_enabled_path",
        "wifi_channel_path",
        "wifi_security_mode_path",
        "wan_pppoe_username_path",
        "wan_pppoe_password_path",
        "wan_connection_type_path",
        "default_wifi_security_mode",
        "default_wifi_channel",
        "min_firmware_version",
    ]

    for col in columns_to_drop:
        if col in onu_type_columns:
            op.drop_column("onu_types", col)
