"""Remove legacy OLT config_pack profile and GEM defaults.

Revision ID: 092_remove_legacy_olt_config_pack_profile_defaults
Revises: 091_add_imported_line_profile_gem_mappings
Create Date: 2026-05-06
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "092_remove_legacy_olt_config_pack_profile_defaults"
down_revision = "091_add_imported_line_profile_gem_mappings"
branch_labels = None
depends_on = None


LEGACY_CONFIG_PACK_KEYS = (
    "line_profile_id",
    "service_profile_id",
    "internet_gem_index",
    "mgmt_gem_index",
    "voip_gem_index",
    "iptv_gem_index",
)
CONFIG_PACK_CONSTRAINT = "ck_olt_devices_config_pack_required"


def _column_exists(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    columns = inspector.get_columns(table_name)
    return any(column["name"] == column_name for column in columns)


def _constraint_exists(table_name: str, constraint_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(
        constraint.get("name") == constraint_name
        for constraint in inspector.get_check_constraints(table_name)
    )


def upgrade() -> None:
    if not _column_exists("olt_devices", "config_pack"):
        return

    if _constraint_exists("olt_devices", CONFIG_PACK_CONSTRAINT):
        op.drop_constraint(CONFIG_PACK_CONSTRAINT, "olt_devices", type_="check")

    op.create_check_constraint(
        CONFIG_PACK_CONSTRAINT,
        "olt_devices",
        sa.text("""
            config_pack IS NOT NULL
            AND (config_pack->>'internet_vlan_id') IS NOT NULL
            AND (config_pack->>'management_vlan_id') IS NOT NULL
            AND (config_pack->>'tr069_olt_profile_id') IS NOT NULL
        """),
    )

    quoted_keys = ", ".join(f"'{key}'" for key in LEGACY_CONFIG_PACK_KEYS)
    op.execute(
        sa.text(
            f"""
            UPDATE olt_devices
            SET config_pack = COALESCE(config_pack, '{{}}'::jsonb)
                - ARRAY[{quoted_keys}]
            WHERE config_pack IS NOT NULL
              AND config_pack ?| ARRAY[{quoted_keys}]
            """
        )
    )


def downgrade() -> None:
    # Intentionally irreversible: these JSON keys are legacy defaults that are no
    # longer trusted by provisioning. Restoring them would require guessing.
    if _constraint_exists("olt_devices", CONFIG_PACK_CONSTRAINT):
        op.drop_constraint(CONFIG_PACK_CONSTRAINT, "olt_devices", type_="check")
