"""Add ONT type TR-069 path fields and adapter_name.

Revision ID: add_onu_type_tr069_paths
Revises: ffdddc71211e
Create Date: 2026-05-06

Adds:
- adapter_name: Link to code adapter for transforms
- LAN TR-069 paths: lan_ip_address_path, lan_subnet_mask_path, etc.
- Access TR-069 paths: remote_access_enabled_path, http_management_enabled_path
"""

import sqlalchemy as sa

from alembic import op

revision = "add_onu_type_tr069_paths"
down_revision = "087_drop_olt_netconf_columns"
branch_labels = None
depends_on = None


def _column_exists(table_name: str, column_name: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    return column_name in {col["name"] for col in inspector.get_columns(table_name)}


def upgrade() -> None:
    # Add adapter_name for code adapter lookup
    if not _column_exists("onu_types", "adapter_name"):
        op.add_column(
            "onu_types",
            sa.Column("adapter_name", sa.String(80), nullable=True),
        )

    # Add LAN TR-069 paths
    if not _column_exists("onu_types", "lan_ip_address_path"):
        op.add_column(
            "onu_types",
            sa.Column("lan_ip_address_path", sa.String(255), nullable=True),
        )
    if not _column_exists("onu_types", "lan_subnet_mask_path"):
        op.add_column(
            "onu_types",
            sa.Column("lan_subnet_mask_path", sa.String(255), nullable=True),
        )
    if not _column_exists("onu_types", "lan_dhcp_enabled_path"):
        op.add_column(
            "onu_types",
            sa.Column("lan_dhcp_enabled_path", sa.String(255), nullable=True),
        )
    if not _column_exists("onu_types", "lan_dhcp_start_path"):
        op.add_column(
            "onu_types",
            sa.Column("lan_dhcp_start_path", sa.String(255), nullable=True),
        )
    if not _column_exists("onu_types", "lan_dhcp_end_path"):
        op.add_column(
            "onu_types",
            sa.Column("lan_dhcp_end_path", sa.String(255), nullable=True),
        )

    # Add Access/management TR-069 paths
    if not _column_exists("onu_types", "remote_access_enabled_path"):
        op.add_column(
            "onu_types",
            sa.Column("remote_access_enabled_path", sa.String(255), nullable=True),
        )
    if not _column_exists("onu_types", "http_management_enabled_path"):
        op.add_column(
            "onu_types",
            sa.Column("http_management_enabled_path", sa.String(255), nullable=True),
        )


def downgrade() -> None:
    if _column_exists("onu_types", "http_management_enabled_path"):
        op.drop_column("onu_types", "http_management_enabled_path")
    if _column_exists("onu_types", "remote_access_enabled_path"):
        op.drop_column("onu_types", "remote_access_enabled_path")
    if _column_exists("onu_types", "lan_dhcp_end_path"):
        op.drop_column("onu_types", "lan_dhcp_end_path")
    if _column_exists("onu_types", "lan_dhcp_start_path"):
        op.drop_column("onu_types", "lan_dhcp_start_path")
    if _column_exists("onu_types", "lan_dhcp_enabled_path"):
        op.drop_column("onu_types", "lan_dhcp_enabled_path")
    if _column_exists("onu_types", "lan_subnet_mask_path"):
        op.drop_column("onu_types", "lan_subnet_mask_path")
    if _column_exists("onu_types", "lan_ip_address_path"):
        op.drop_column("onu_types", "lan_ip_address_path")
    if _column_exists("onu_types", "adapter_name"):
        op.drop_column("onu_types", "adapter_name")
