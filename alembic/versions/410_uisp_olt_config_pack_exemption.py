"""Exempt UISP-managed OLTs from the TR069 config-pack active constraint.

Ubiquiti UF-OLTs run on the UISP control plane, not TR069, so they never carry a
TR069 config_pack (internet/management VLAN + tr069_olt_profile_id). The
``ck_olt_devices_config_pack_required`` check was Huawei/TR069-shaped and blocked
activating live UISP OLTs (leaving their subscribers' ONT assignments pointing at
an "inactive" OLT). A UISP-managed OLT (``uisp_device_id IS NOT NULL``) is
valid-active without a TR069 config pack.

Revision ID: 410_uisp_olt_config_pack_exemption
Revises: 409_tr069_operation_lifecycle
Create Date: 2026-07-23
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "410_uisp_olt_config_pack_exemption"
down_revision = "409_tr069_operation_lifecycle"
branch_labels = None
depends_on = None

CONSTRAINT_NAME = "ck_olt_devices_config_pack_required"

_TR069_PACK_COMPLETE = """
    config_pack IS NOT NULL
    AND (config_pack->>'internet_vlan_id') IS NOT NULL
    AND (config_pack->>'management_vlan_id') IS NOT NULL
    AND (config_pack->>'tr069_olt_profile_id') IS NOT NULL
"""


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
        sa.text(f"""
            NOT is_active
            OR uisp_device_id IS NOT NULL
            OR ({_TR069_PACK_COMPLETE})
        """),
    )


def downgrade() -> None:
    if _constraint_exists("olt_devices", CONSTRAINT_NAME):
        op.drop_constraint(CONSTRAINT_NAME, "olt_devices", type_="check")

    op.create_check_constraint(
        CONSTRAINT_NAME,
        "olt_devices",
        sa.text(f"""
            NOT is_active
            OR ({_TR069_PACK_COMPLETE})
        """),
    )
