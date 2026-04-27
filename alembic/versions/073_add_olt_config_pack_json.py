"""Add config_pack JSON column to OLT devices and populate from existing columns.

This migration consolidates 22 scattered config pack columns into a single JSON field
that becomes the source of truth for OLT provisioning defaults.

Revision ID: 073_add_olt_config_pack_json
Revises: 072_remove_parallel_ont_config_sources
Create Date: 2026-04-27
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "073_add_olt_config_pack_json"
down_revision = "072_remove_parallel_ont_config_sources"
branch_labels = None
depends_on = None


def _column_exists(inspector: sa.Inspector, table: str, column: str) -> bool:
    return any(col["name"] == column for col in inspector.get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # Add config_pack JSON column if it doesn't exist
    if not _column_exists(inspector, "olt_devices", "config_pack"):
        op.add_column(
            "olt_devices",
            sa.Column(
                "config_pack",
                JSONB(),
                nullable=True,
                server_default="{}",
            ),
        )

    # Populate config_pack from existing columns
    # VLAN references are stored as UUID strings for later resolution
    op.execute(
        """
        UPDATE olt_devices
        SET config_pack = jsonb_build_object(
            'line_profile_id', default_line_profile_id,
            'service_profile_id', default_service_profile_id,
            'tr069_olt_profile_id', default_tr069_olt_profile_id,
            'internet_vlan_id', internet_vlan_id::text,
            'management_vlan_id', management_vlan_id::text,
            'tr069_vlan_id', tr069_vlan_id::text,
            'voip_vlan_id', voip_vlan_id::text,
            'iptv_vlan_id', iptv_vlan_id::text,
            'internet_config_ip_index', COALESCE(default_internet_config_ip_index, 0),
            'wan_config_profile_id', COALESCE(default_wan_config_profile_id, 0),
            'cr_username', default_cr_username,
            'cr_password', default_cr_password,
            'internet_gem_index', COALESCE(default_internet_gem_index, 1),
            'mgmt_gem_index', COALESCE(default_mgmt_gem_index, 2),
            'voip_gem_index', COALESCE(default_voip_gem_index, 3),
            'iptv_gem_index', COALESCE(default_iptv_gem_index, 4),
            'mgmt_traffic_table_inbound', mgmt_traffic_table_inbound,
            'mgmt_traffic_table_outbound', mgmt_traffic_table_outbound,
            'internet_traffic_table_inbound', internet_traffic_table_inbound,
            'internet_traffic_table_outbound', internet_traffic_table_outbound,
            'pppoe_wcd_index', COALESCE(pppoe_wcd_index, 2),
            'mgmt_wcd_index', COALESCE(mgmt_wcd_index, 1),
            'voip_wcd_index', voip_wcd_index
        )
        WHERE config_pack IS NULL OR config_pack = '{}'::jsonb
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # Drop config_pack column if it exists
    if _column_exists(inspector, "olt_devices", "config_pack"):
        op.drop_column("olt_devices", "config_pack")
