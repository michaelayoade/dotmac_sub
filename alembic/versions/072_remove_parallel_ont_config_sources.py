"""Remove parallel ONT config sources owned by OLT config pack.

Revision ID: 072_remove_parallel_ont_config_sources
Revises: 071_strip_ont_desired_config_pack_bloat
Create Date: 2026-04-26
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "072_remove_parallel_ont_config_sources"
down_revision = "071_strip_ont_desired_config_pack_bloat"
branch_labels = None
depends_on = None


def _column_exists(inspector: sa.Inspector, table: str, column: str) -> bool:
    return any(col["name"] == column for col in inspector.get_columns(table))


def _drop_index_if_exists(inspector: sa.Inspector, table: str, index_name: str) -> None:
    if any(index.get("name") == index_name for index in inspector.get_indexes(table)):
        op.drop_index(index_name, table_name=table)


def _drop_fk_for_column(inspector: sa.Inspector, table: str, column: str) -> None:
    for fk in inspector.get_foreign_keys(table):
        if column in (fk.get("constrained_columns") or []) and fk.get("name"):
            op.drop_constraint(fk["name"], table, type_="foreignkey")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    assignment_columns = {
        col["name"] for col in inspector.get_columns("ont_assignments")
    }
    for name, column_type in (
        ("static_dns", sa.String(length=200)),
        ("mgmt_subnet", sa.String(length=64)),
        ("mgmt_gateway", sa.String(length=64)),
        ("lan_ip", sa.String(length=64)),
        ("lan_subnet", sa.String(length=64)),
        ("lan_dhcp_enabled", sa.Boolean()),
        ("lan_dhcp_start", sa.String(length=64)),
        ("lan_dhcp_end", sa.String(length=64)),
        ("wifi_enabled", sa.Boolean()),
        ("wifi_security_mode", sa.String(length=40)),
        ("wifi_channel", sa.String(length=10)),
    ):
        if name not in assignment_columns:
            op.add_column("ont_assignments", sa.Column(name, column_type, nullable=True))

    for column, index_name in (
        ("internet_vlan_id", "ix_ont_assignments_internet_vlan_id"),
        ("mgmt_vlan_id", "ix_ont_assignments_mgmt_vlan_id"),
    ):
        if _column_exists(inspector, "ont_assignments", column):
            _drop_fk_for_column(inspector, "ont_assignments", column)
            _drop_index_if_exists(inspector, "ont_assignments", index_name)
            op.drop_column("ont_assignments", column)

    if _column_exists(inspector, "ont_units", "tr069_olt_profile_id"):
        op.drop_column("ont_units", "tr069_olt_profile_id")


def downgrade() -> None:
    # Removed parallel source-of-truth columns cannot be reconstructed safely.
    pass
