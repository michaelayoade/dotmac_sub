"""Drop OLT config pack columns now that config_pack JSON is source of truth.

This migration removes the 23 columns that have been consolidated into
the config_pack JSON field. The columns were already migrated in migration 073.

Revision ID: 074_drop_olt_config_pack_columns
Revises: 073_add_olt_config_pack_json
Create Date: 2026-04-27
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "074_drop_olt_config_pack_columns"
down_revision = "073_add_olt_config_pack_json"
branch_labels = None
depends_on = None

# Columns to drop (23 total)
COLUMNS_TO_DROP = [
    # Authorization profiles
    "default_line_profile_id",
    "default_service_profile_id",
    # TR-069 OLT profile
    "default_tr069_olt_profile_id",
    # VLAN FKs
    "internet_vlan_id",
    "management_vlan_id",
    "tr069_vlan_id",
    "voip_vlan_id",
    "iptv_vlan_id",
    # Provisioning knobs
    "default_internet_config_ip_index",
    "default_wan_config_profile_id",
    # Connection request credentials
    "default_cr_username",
    "default_cr_password",
    # GEM indices
    "default_internet_gem_index",
    "default_mgmt_gem_index",
    "default_voip_gem_index",
    "default_iptv_gem_index",
    # Traffic table indices
    "mgmt_traffic_table_inbound",
    "mgmt_traffic_table_outbound",
    "internet_traffic_table_inbound",
    "internet_traffic_table_outbound",
    # WCD indices
    "pppoe_wcd_index",
    "mgmt_wcd_index",
    "voip_wcd_index",
]

# VLAN FK columns need special handling (drop FK constraint first)
VLAN_FK_COLUMNS = [
    "internet_vlan_id",
    "management_vlan_id",
    "tr069_vlan_id",
    "voip_vlan_id",
    "iptv_vlan_id",
]


def _column_exists(inspector: sa.Inspector, table: str, column: str) -> bool:
    return any(col["name"] == column for col in inspector.get_columns(table))


def _drop_fk_for_column(inspector: sa.Inspector, table: str, column: str) -> None:
    """Drop FK constraint for a column if it exists."""
    for fk in inspector.get_foreign_keys(table):
        if column in (fk.get("constrained_columns") or []) and fk.get("name"):
            op.drop_constraint(fk["name"], table, type_="foreignkey")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # First drop FK constraints for VLAN columns
    for column in VLAN_FK_COLUMNS:
        if _column_exists(inspector, "olt_devices", column):
            _drop_fk_for_column(inspector, "olt_devices", column)

    # Then drop all columns
    for column in COLUMNS_TO_DROP:
        if _column_exists(inspector, "olt_devices", column):
            op.drop_column("olt_devices", column)


def downgrade() -> None:
    # Recreating 23 columns would require re-extracting data from config_pack JSON.
    # This is a one-way migration; use a backup to restore if needed.
    pass
