"""Allow inactive OLT placeholders without config packs.

Revision ID: 154_relax_inactive_olt_config_pack_constraint
Revises: 153_topology_zabbix_linkage
Create Date: 2026-06-17
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "154_relax_inactive_olt_config_pack_constraint"
down_revision = "153_topology_zabbix_linkage"
branch_labels = None
depends_on = None

CONSTRAINT_NAME = "ck_olt_devices_config_pack_required"


def _constraint_exists(table_name: str, constraint_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(
        constraint.get("name") == constraint_name
        for constraint in inspector.get_check_constraints(table_name)
    )


def upgrade() -> None:
    if _constraint_exists("olt_devices", CONSTRAINT_NAME):
        op.drop_constraint(CONSTRAINT_NAME, "olt_devices", type_="check")

    op.create_check_constraint(
        CONSTRAINT_NAME,
        "olt_devices",
        sa.text("""
            NOT is_active
            OR (
                config_pack IS NOT NULL
                AND (config_pack->>'internet_vlan_id') IS NOT NULL
                AND (config_pack->>'management_vlan_id') IS NOT NULL
                AND (config_pack->>'tr069_olt_profile_id') IS NOT NULL
            )
        """),
    )


def downgrade() -> None:
    if _constraint_exists("olt_devices", CONSTRAINT_NAME):
        op.drop_constraint(CONSTRAINT_NAME, "olt_devices", type_="check")

    op.create_check_constraint(
        CONSTRAINT_NAME,
        "olt_devices",
        sa.text("""
            config_pack IS NOT NULL
            AND (config_pack->>'internet_vlan_id') IS NOT NULL
            AND (config_pack->>'management_vlan_id') IS NOT NULL
            AND (config_pack->>'tr069_olt_profile_id') IS NOT NULL
        """),
    )
