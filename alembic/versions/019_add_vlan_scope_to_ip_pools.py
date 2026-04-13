"""add vlan scope to ip pools

Revision ID: 019_add_vlan_scope_to_ip_pools
Revises: 018_remove_implicit_ont_profile_index_defaults
Create Date: 2026-04-13
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "019_add_vlan_scope_to_ip_pools"
down_revision = "018_remove_implicit_ont_profile_index_defaults"
branch_labels = None
depends_on = None


def _columns(table_name: str) -> set[str]:
    inspector = inspect(op.get_bind())
    return {column["name"] for column in inspector.get_columns(table_name)}


def _indexes(table_name: str) -> set[str]:
    inspector = inspect(op.get_bind())
    return {index["name"] for index in inspector.get_indexes(table_name)}


def _foreign_keys(table_name: str) -> set[str]:
    inspector = inspect(op.get_bind())
    return {fk["name"] for fk in inspector.get_foreign_keys(table_name)}


def upgrade() -> None:
    columns = _columns("ip_pools")
    if "vlan_id" not in columns:
        op.add_column("ip_pools", sa.Column("vlan_id", sa.UUID(), nullable=True))

    indexes = _indexes("ip_pools")
    if "ix_ip_pools_vlan_id" not in indexes:
        op.create_index("ix_ip_pools_vlan_id", "ip_pools", ["vlan_id"])

    foreign_keys = _foreign_keys("ip_pools")
    if "fk_ip_pools_vlan_id_vlans" not in foreign_keys:
        op.create_foreign_key(
            "fk_ip_pools_vlan_id_vlans",
            "ip_pools",
            "vlans",
            ["vlan_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    foreign_keys = _foreign_keys("ip_pools")
    if "fk_ip_pools_vlan_id_vlans" in foreign_keys:
        op.drop_constraint("fk_ip_pools_vlan_id_vlans", "ip_pools", type_="foreignkey")

    indexes = _indexes("ip_pools")
    if "ix_ip_pools_vlan_id" in indexes:
        op.drop_index("ix_ip_pools_vlan_id", table_name="ip_pools")

    columns = _columns("ip_pools")
    if "vlan_id" in columns:
        op.drop_column("ip_pools", "vlan_id")
