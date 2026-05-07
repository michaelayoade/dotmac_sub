"""Add WAN behavior to OLT ONU type mappings.

Revision ID: 097_add_wan_behavior_to_olt_onu_type_mappings
Revises: 096_add_imported_olt_service_ports
Create Date: 2026-05-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "097_add_wan_behavior_to_olt_onu_type_mappings"
down_revision = "096_add_imported_olt_service_ports"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(
        column["name"] == column_name
        for column in inspector.get_columns(table_name)
    )


def upgrade() -> None:
    columns = [
        ("wan_provisioning_mode", sa.String(length=40)),
        ("internet_config_ip_index", sa.Integer()),
        ("wan_config_profile_id", sa.Integer()),
        ("pppoe_wcd_index", sa.Integer()),
        ("mgmt_wcd_index", sa.Integer()),
        ("voip_wcd_index", sa.Integer()),
        ("primary_wan_service", sa.String(length=40)),
    ]
    for name, type_ in columns:
        if not _has_column("olt_onu_type_profile_mappings", name):
            op.add_column(
                "olt_onu_type_profile_mappings",
                sa.Column(name, type_, nullable=True),
            )


def downgrade() -> None:
    for name in [
        "primary_wan_service",
        "voip_wcd_index",
        "mgmt_wcd_index",
        "pppoe_wcd_index",
        "wan_config_profile_id",
        "internet_config_ip_index",
        "wan_provisioning_mode",
    ]:
        if _has_column("olt_onu_type_profile_mappings", name):
            op.drop_column("olt_onu_type_profile_mappings", name)
