"""Add CHECK constraint for required OLT config_pack fields.

This migration ensures all OLTs have complete config_pack fields required
for ONT provisioning. The constraint validates:
- Authorization: line_profile_id, service_profile_id
- VLANs: internet_vlan_id, management_vlan_id
- TR-069: tr069_olt_profile_id
- GEM indices: mgmt_gem_index, internet_gem_index

Revision ID: 076_add_olt_config_pack_check_constraint
Revises: 075_add_ont_unit_pon_port_id
Create Date: 2026-04-27
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "076_add_olt_config_pack_check_constraint"
down_revision = "075_add_ont_unit_pon_port_id"
branch_labels = None
depends_on = None

CONSTRAINT_NAME = "ck_olt_devices_config_pack_required"


def _constraint_exists(inspector: sa.Inspector, table: str, constraint: str) -> bool:
    """Check if a check constraint exists."""
    for ck in inspector.get_check_constraints(table):
        if ck.get("name") == constraint:
            return True
    return False


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _constraint_exists(inspector, "olt_devices", CONSTRAINT_NAME):
        return

    # Add CHECK constraint for required config_pack fields
    # Using JSONB operators to validate required keys exist and are not null
    op.create_check_constraint(
        CONSTRAINT_NAME,
        "olt_devices",
        sa.text("""
            config_pack IS NOT NULL
            AND (config_pack->>'line_profile_id') IS NOT NULL
            AND (config_pack->>'service_profile_id') IS NOT NULL
            AND (config_pack->>'internet_vlan_id') IS NOT NULL
            AND (config_pack->>'management_vlan_id') IS NOT NULL
            AND (config_pack->>'tr069_olt_profile_id') IS NOT NULL
            AND (config_pack->>'mgmt_gem_index') IS NOT NULL
            AND (config_pack->>'internet_gem_index') IS NOT NULL
        """),
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _constraint_exists(inspector, "olt_devices", CONSTRAINT_NAME):
        op.drop_constraint(CONSTRAINT_NAME, "olt_devices", type_="check")
