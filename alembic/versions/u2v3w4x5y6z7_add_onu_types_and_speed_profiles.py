"""Add ONU type catalog, speed profiles, and enhanced OntUnit fields.

Revision ID: u2v3w4x5y6z7
Revises: t1u2v3w4x5y6
Create Date: 2026-02-25 14:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "u2v3w4x5y6z7"
down_revision = "t1u2v3w4x5y6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- Enum types (idempotent) ---
    for enum_name, values in [
        ("pontype", ["gpon", "epon"]),
        ("gponchannel", ["gpon", "xg_pon", "xgs_pon"]),
        ("onucapability", ["bridging", "routing", "bridging_routing"]),
        ("onumode", ["routing", "bridging"]),
        ("wanmode", ["dhcp", "static_ip", "pppoe", "setup_via_onu"]),
        ("configmethod", ["omci", "tr069"]),
        ("ipprotocol", ["ipv4", "dual_stack"]),
        ("mgmtipmode", ["inactive", "static_ip", "dhcp"]),
        ("speedprofiledirection", ["download", "upload"]),
        ("speedprofiletype", ["internet", "management"]),
    ]:
        enum = postgresql.ENUM(*values, name=enum_name, create_type=False)
        enum.create(op.get_bind(), checkfirst=True)

    # --- onu_types table ---
    op.create_table(
        "onu_types",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column(
            "pon_type",
            postgresql.ENUM("gpon", "epon", name="pontype", create_type=False),
            nullable=False,
            server_default="gpon",
        ),
        sa.Column(
            "gpon_channel",
            postgresql.ENUM("gpon", "xg_pon", "xgs_pon", name="gponchannel", create_type=False),
            nullable=False,
            server_default="gpon",
        ),
        sa.Column("ethernet_ports", sa.Integer, server_default="0"),
        sa.Column("wifi_ports", sa.Integer, server_default="0"),
        sa.Column("voip_ports", sa.Integer, server_default="0"),
        sa.Column("catv_ports", sa.Integer, server_default="0"),
        sa.Column("allow_custom_profiles", sa.Boolean, server_default=sa.text("true")),
        sa.Column(
            "capability",
            postgresql.ENUM(
                "bridging", "routing", "bridging_routing",
                name="onucapability", create_type=False,
            ),
            nullable=False,
            server_default="bridging_routing",
        ),
        sa.Column("is_active", sa.Boolean, server_default=sa.text("true")),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.UniqueConstraint("name", name="uq_onu_types_name"),
    )

    # --- speed_profiles table ---
    op.create_table(
        "speed_profiles",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column(
            "direction",
            postgresql.ENUM("download", "upload", name="speedprofiledirection", create_type=False),
            nullable=False,
        ),
        sa.Column("speed_kbps", sa.Integer, nullable=False),
        sa.Column(
            "speed_type",
            postgresql.ENUM("internet", "management", name="speedprofiletype", create_type=False),
            nullable=False,
            server_default="internet",
        ),
        sa.Column("use_prefix_suffix", sa.Boolean, server_default=sa.text("false")),
        sa.Column("is_default", sa.Boolean, server_default=sa.text("false")),
        sa.Column("is_active", sa.Boolean, server_default=sa.text("true")),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.UniqueConstraint("name", "direction", name="uq_speed_profiles_name_direction"),
    )

    # --- Extend ont_units with SmartOLT fields ---
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_cols = {c["name"] for c in inspector.get_columns("ont_units")}

    new_columns = [
        ("onu_type_id", sa.dialects.postgresql.UUID(as_uuid=True), {"nullable": True}),
        ("olt_device_id", sa.dialects.postgresql.UUID(as_uuid=True), {"nullable": True}),
        ("pon_type", postgresql.ENUM("gpon", "epon", name="pontype", create_type=False), {"nullable": True}),
        ("gpon_channel", postgresql.ENUM("gpon", "xg_pon", "xgs_pon", name="gponchannel", create_type=False), {"nullable": True}),
        ("board", sa.String(60), {"nullable": True}),
        ("port", sa.String(60), {"nullable": True}),
        ("onu_mode", postgresql.ENUM("routing", "bridging", name="onumode", create_type=False), {"nullable": True}),
        ("user_vlan_id", sa.dialects.postgresql.UUID(as_uuid=True), {"nullable": True}),
        ("splitter_id", sa.dialects.postgresql.UUID(as_uuid=True), {"nullable": True}),
        ("splitter_port_id", sa.dialects.postgresql.UUID(as_uuid=True), {"nullable": True}),
        ("download_speed_profile_id", sa.dialects.postgresql.UUID(as_uuid=True), {"nullable": True}),
        ("upload_speed_profile_id", sa.dialects.postgresql.UUID(as_uuid=True), {"nullable": True}),
        ("name", sa.String(200), {"nullable": True}),
        ("address_or_comment", sa.Text, {"nullable": True}),
        ("external_id", sa.String(120), {"nullable": True}),
        ("use_gps", sa.Boolean, {"server_default": sa.text("false")}),
        ("gps_latitude", sa.Float, {"nullable": True}),
        ("gps_longitude", sa.Float, {"nullable": True}),
        # ONU mode configuration
        ("wan_vlan_id", sa.dialects.postgresql.UUID(as_uuid=True), {"nullable": True}),
        ("wan_mode", postgresql.ENUM("dhcp", "static_ip", "pppoe", "setup_via_onu", name="wanmode", create_type=False), {"nullable": True}),
        ("config_method", postgresql.ENUM("omci", "tr069", name="configmethod", create_type=False), {"nullable": True}),
        ("ip_protocol", postgresql.ENUM("ipv4", "dual_stack", name="ipprotocol", create_type=False), {"nullable": True}),
        ("pppoe_username", sa.String(120), {"nullable": True}),
        ("pppoe_password", sa.String(120), {"nullable": True}),
        ("wan_remote_access", sa.Boolean, {"server_default": sa.text("false")}),
        # Management IP configuration
        ("tr069_acs_server_id", sa.dialects.postgresql.UUID(as_uuid=True), {"nullable": True}),
        ("mgmt_ip_mode", postgresql.ENUM("inactive", "static_ip", "dhcp", name="mgmtipmode", create_type=False), {"nullable": True}),
        ("mgmt_vlan_id", sa.dialects.postgresql.UUID(as_uuid=True), {"nullable": True}),
        ("mgmt_ip_address", sa.String(64), {"nullable": True}),
        ("mgmt_remote_access", sa.Boolean, {"server_default": sa.text("false")}),
        ("voip_enabled", sa.Boolean, {"server_default": sa.text("false")}),
    ]

    for col_name, col_type, kwargs in new_columns:
        if col_name not in existing_cols:
            op.add_column("ont_units", sa.Column(col_name, col_type, **kwargs))

    # Foreign keys on ont_units
    fk_defs = [
        ("fk_ont_units_onu_type_id", "onu_type_id", "onu_types", "id"),
        ("fk_ont_units_olt_device_id", "olt_device_id", "olt_devices", "id"),
        ("fk_ont_units_user_vlan_id", "user_vlan_id", "vlans", "id"),
        ("fk_ont_units_splitter_id", "splitter_id", "splitters", "id"),
        ("fk_ont_units_splitter_port_id", "splitter_port_id", "splitter_ports", "id"),
        ("fk_ont_units_dl_speed_id", "download_speed_profile_id", "speed_profiles", "id"),
        ("fk_ont_units_ul_speed_id", "upload_speed_profile_id", "speed_profiles", "id"),
        ("fk_ont_units_wan_vlan_id", "wan_vlan_id", "vlans", "id"),
        ("fk_ont_units_tr069_acs_id", "tr069_acs_server_id", "tr069_acs_servers", "id"),
        ("fk_ont_units_mgmt_vlan_id", "mgmt_vlan_id", "vlans", "id"),
    ]
    existing_fks = {fk["name"] for fk in inspector.get_foreign_keys("ont_units") if fk.get("name")}
    for fk_name, col, ref_table, ref_col in fk_defs:
        if fk_name not in existing_fks:
            op.create_foreign_key(fk_name, "ont_units", ref_table, [col], [ref_col])


def downgrade() -> None:
    # Drop foreign keys from ont_units
    fk_names = [
        "fk_ont_units_mgmt_vlan_id",
        "fk_ont_units_tr069_acs_id",
        "fk_ont_units_wan_vlan_id",
        "fk_ont_units_ul_speed_id",
        "fk_ont_units_dl_speed_id",
        "fk_ont_units_splitter_port_id",
        "fk_ont_units_splitter_id",
        "fk_ont_units_user_vlan_id",
        "fk_ont_units_olt_device_id",
        "fk_ont_units_onu_type_id",
    ]
    for fk_name in fk_names:
        op.drop_constraint(fk_name, "ont_units", type_="foreignkey")

    # Drop added columns from ont_units
    cols_to_drop = [
        "voip_enabled", "mgmt_remote_access", "mgmt_ip_address", "mgmt_vlan_id",
        "mgmt_ip_mode", "tr069_acs_server_id", "wan_remote_access", "pppoe_password",
        "pppoe_username", "ip_protocol", "config_method", "wan_mode", "wan_vlan_id",
        "gps_longitude", "gps_latitude", "use_gps", "external_id", "address_or_comment",
        "name", "upload_speed_profile_id", "download_speed_profile_id", "splitter_port_id",
        "splitter_id", "user_vlan_id", "onu_mode", "port", "board", "gpon_channel",
        "pon_type", "olt_device_id", "onu_type_id",
    ]
    for col_name in cols_to_drop:
        op.drop_column("ont_units", col_name)

    op.drop_table("speed_profiles")
    op.drop_table("onu_types")

    for enum_name in [
        "speedprofiletype", "speedprofiledirection", "mgmtipmode", "ipprotocol",
        "configmethod", "wanmode", "onumode", "onucapability", "gponchannel", "pontype",
    ]:
        postgresql.ENUM(name=enum_name).drop(op.get_bind(), checkfirst=True)
