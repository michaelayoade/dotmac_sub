"""router management tables

Revision ID: 005_router_management
Revises: d1d2d3d4d5d6
Create Date: 2026-03-29
"""

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSON, UUID

from alembic import op

revision = "005_router_management"
down_revision = "d1d2d3d4d5d6"
branch_labels = None
depends_on = None


def _table_exists(name: str) -> bool:
    conn = op.get_bind()
    insp = inspect(conn)
    return name in insp.get_table_names()


def _add_enum_value_if_not_exists(enum_name: str, value: str) -> None:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM pg_enum WHERE enumlabel = :val "
            "AND enumtypid = (SELECT oid FROM pg_type WHERE typname = :name)"
        ),
        {"val": value, "name": enum_name},
    )
    if result.fetchone() is None:
        # ALTER TYPE ADD VALUE does not accept bound parameters — inline the literal safely
        conn.execute(
            sa.text(f"ALTER TYPE {enum_name} ADD VALUE IF NOT EXISTS '{value}'")
        )


def upgrade() -> None:
    if not _table_exists("jump_hosts"):
        op.create_table(
            "jump_hosts",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("name", sa.String(255), unique=True, nullable=False),
            sa.Column("hostname", sa.String(255), nullable=False),
            sa.Column("port", sa.Integer, server_default="22"),
            sa.Column("username", sa.String(255), nullable=False),
            sa.Column("ssh_key", sa.Text, nullable=True),
            sa.Column("ssh_password", sa.String(512), nullable=True),
            sa.Column("is_active", sa.Boolean, server_default="true"),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()
            ),
        )

    if not _table_exists("routers"):
        op.create_table(
            "routers",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("name", sa.String(255), unique=True, nullable=False),
            sa.Column("hostname", sa.String(255), nullable=False),
            sa.Column("management_ip", sa.String(255), nullable=False),
            sa.Column("rest_api_port", sa.Integer, server_default="443"),
            sa.Column("rest_api_username", sa.String(255), nullable=False),
            sa.Column("rest_api_password", sa.String(512), nullable=False),
            sa.Column("use_ssl", sa.Boolean, server_default="true"),
            sa.Column("verify_tls", sa.Boolean, server_default="false"),
            sa.Column("routeros_version", sa.String(50), nullable=True),
            sa.Column("board_name", sa.String(100), nullable=True),
            sa.Column("architecture", sa.String(50), nullable=True),
            sa.Column("serial_number", sa.String(100), nullable=True),
            sa.Column("firmware_type", sa.String(50), nullable=True),
            sa.Column("location", sa.String(255), nullable=True),
            sa.Column("notes", sa.Text, nullable=True),
            sa.Column("tags", JSON, nullable=True),
            sa.Column("access_method", sa.String(20), server_default="direct"),
            sa.Column(
                "jump_host_id",
                UUID(as_uuid=True),
                sa.ForeignKey("jump_hosts.id"),
                nullable=True,
            ),
            sa.Column(
                "nas_device_id",
                UUID(as_uuid=True),
                sa.ForeignKey("nas_devices.id"),
                nullable=True,
            ),
            sa.Column(
                "network_device_id",
                UUID(as_uuid=True),
                sa.ForeignKey("network_devices.id"),
                nullable=True,
            ),
            sa.Column("status", sa.String(20), server_default="offline"),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_config_sync_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "last_config_change_at", sa.DateTime(timezone=True), nullable=True
            ),
            sa.Column("reseller_id", UUID(as_uuid=True), nullable=True),
            sa.Column("organization_id", UUID(as_uuid=True), nullable=True),
            sa.Column("is_active", sa.Boolean, server_default="true"),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()
            ),
        )
        op.create_index("ix_routers_status", "routers", ["status"])
        op.create_index("ix_routers_management_ip", "routers", ["management_ip"])

    if not _table_exists("router_interfaces"):
        op.create_table(
            "router_interfaces",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "router_id",
                UUID(as_uuid=True),
                sa.ForeignKey("routers.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("name", sa.String(100), nullable=False),
            sa.Column("type", sa.String(50), server_default="ether"),
            sa.Column("mac_address", sa.String(17), nullable=True),
            sa.Column("is_running", sa.Boolean, server_default="false"),
            sa.Column("is_disabled", sa.Boolean, server_default="false"),
            sa.Column("rx_byte", sa.BigInteger, server_default="0"),
            sa.Column("tx_byte", sa.BigInteger, server_default="0"),
            sa.Column("rx_packet", sa.BigInteger, server_default="0"),
            sa.Column("tx_packet", sa.BigInteger, server_default="0"),
            sa.Column("last_link_up_time", sa.String(100), nullable=True),
            sa.Column("speed", sa.String(50), nullable=True),
            sa.Column("comment", sa.String(255), nullable=True),
            sa.Column(
                "synced_at", sa.DateTime(timezone=True), server_default=sa.func.now()
            ),
        )
        op.create_unique_constraint(
            "uq_router_interface_name", "router_interfaces", ["router_id", "name"]
        )

    if not _table_exists("router_config_snapshots"):
        op.create_table(
            "router_config_snapshots",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "router_id",
                UUID(as_uuid=True),
                sa.ForeignKey("routers.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("config_export", sa.Text, nullable=False),
            sa.Column("config_hash", sa.String(64), nullable=False),
            sa.Column("source", sa.String(20), nullable=False),
            sa.Column("captured_by", UUID(as_uuid=True), nullable=True),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
            ),
        )
        op.create_index(
            "ix_router_config_snapshots_router_id",
            "router_config_snapshots",
            ["router_id"],
        )

    if not _table_exists("router_config_templates"):
        op.create_table(
            "router_config_templates",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("name", sa.String(255), unique=True, nullable=False),
            sa.Column("description", sa.Text, nullable=True),
            sa.Column("template_body", sa.Text, nullable=False),
            sa.Column("category", sa.String(20), server_default="custom"),
            sa.Column("variables", JSON, server_default="{}"),
            sa.Column("is_active", sa.Boolean, server_default="true"),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()
            ),
        )

    if not _table_exists("router_config_pushes"):
        op.create_table(
            "router_config_pushes",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "template_id",
                UUID(as_uuid=True),
                sa.ForeignKey("router_config_templates.id"),
                nullable=True,
            ),
            sa.Column("commands", JSON, nullable=False),
            sa.Column("variable_values", JSON, nullable=True),
            sa.Column("initiated_by", UUID(as_uuid=True), nullable=False),
            sa.Column("status", sa.String(20), server_default="pending"),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
            ),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        )

    if not _table_exists("router_config_push_results"):
        op.create_table(
            "router_config_push_results",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "push_id",
                UUID(as_uuid=True),
                sa.ForeignKey("router_config_pushes.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "router_id",
                UUID(as_uuid=True),
                sa.ForeignKey("routers.id"),
                nullable=False,
            ),
            sa.Column("status", sa.String(20), server_default="pending"),
            sa.Column("response_data", JSON, nullable=True),
            sa.Column("error_message", sa.Text, nullable=True),
            sa.Column(
                "pre_snapshot_id",
                UUID(as_uuid=True),
                sa.ForeignKey("router_config_snapshots.id"),
                nullable=True,
            ),
            sa.Column(
                "post_snapshot_id",
                UUID(as_uuid=True),
                sa.ForeignKey("router_config_snapshots.id"),
                nullable=True,
            ),
            sa.Column("duration_ms", sa.Integer, nullable=True),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
            ),
        )
        op.create_index(
            "ix_push_results_push_id", "router_config_push_results", ["push_id"]
        )

    for val in [
        "router_config_push",
        "router_config_backup",
        "router_reboot",
        "router_firmware_upgrade",
        "router_bulk_push",
    ]:
        _add_enum_value_if_not_exists("networkoperationtype", val)


def downgrade() -> None:
    op.drop_table("router_config_push_results")
    op.drop_table("router_config_pushes")
    op.drop_table("router_config_templates")
    op.drop_table("router_config_snapshots")
    op.drop_table("router_interfaces")
    op.drop_table("routers")
    op.drop_table("jump_hosts")
