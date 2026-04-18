"""harden network edge constraints

Revision ID: 029_harden_network_edge_constraints
Revises: 028_scope_ont_serial_uniqueness_to_olt
Create Date: 2026-04-17
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect, text

from alembic import op

revision = "029_harden_network_edge_constraints"
down_revision = "028_scope_ont_serial_uniqueness_to_olt"
branch_labels = None
depends_on = None


def _columns(table_name: str) -> set[str]:
    inspector = inspect(op.get_bind())
    return {column["name"] for column in inspector.get_columns(table_name)}


def _constraints(table_name: str) -> set[str]:
    inspector = inspect(op.get_bind())
    names = {constraint["name"] for constraint in inspector.get_check_constraints(table_name)}
    names.update(
        constraint["name"]
        for constraint in inspector.get_unique_constraints(table_name)
        if constraint.get("name")
    )
    return names


def _indexes(table_name: str) -> set[str]:
    inspector = inspect(op.get_bind())
    return {index["name"] for index in inspector.get_indexes(table_name)}


def upgrade() -> None:
    if "max_ont_capacity" not in _columns("pon_ports"):
        op.add_column("pon_ports", sa.Column("max_ont_capacity", sa.Integer(), nullable=True))

    constraints = _constraints("pon_ports")
    if "ck_pon_ports_max_ont_capacity_positive" not in constraints:
        op.create_check_constraint(
            "ck_pon_ports_max_ont_capacity_positive",
            "pon_ports",
            "max_ont_capacity IS NULL OR max_ont_capacity > 0",
        )

    op.execute(
        text(
            """
            UPDATE ont_units
            SET mac_address = NULL
            WHERE mac_address IS NOT NULL
              AND mac_address !~* '^([0-9a-f]{2}:){5}[0-9a-f]{2}$'
            """
        )
    )

    constraints = _constraints("ont_units")
    if "ck_ont_units_mac_address_format" not in constraints:
        op.create_check_constraint(
            "ck_ont_units_mac_address_format",
            "ont_units",
            "mac_address IS NULL OR mac_address ~* '^([0-9a-f]{2}:){5}[0-9a-f]{2}$'",
        )
    if "ck_ont_units_tr069_snapshot_object" not in constraints:
        op.create_check_constraint(
            "ck_ont_units_tr069_snapshot_object",
            "ont_units",
            "tr069_last_snapshot IS NULL OR jsonb_typeof(tr069_last_snapshot::jsonb) = 'object'",
        )

    indexes = _indexes("vlans")
    if "uq_vlans_region_global_tag" not in indexes:
        op.create_index(
            "uq_vlans_region_global_tag",
            "vlans",
            ["region_id", "tag"],
            unique=True,
            postgresql_where=sa.text("olt_device_id IS NULL"),
        )


def downgrade() -> None:
    indexes = _indexes("vlans")
    if "uq_vlans_region_global_tag" in indexes:
        op.drop_index("uq_vlans_region_global_tag", table_name="vlans")

    constraints = _constraints("ont_units")
    if "ck_ont_units_tr069_snapshot_object" in constraints:
        op.drop_constraint("ck_ont_units_tr069_snapshot_object", "ont_units", type_="check")
    if "ck_ont_units_mac_address_format" in constraints:
        op.drop_constraint("ck_ont_units_mac_address_format", "ont_units", type_="check")

    constraints = _constraints("pon_ports")
    if "ck_pon_ports_max_ont_capacity_positive" in constraints:
        op.drop_constraint(
            "ck_pon_ports_max_ont_capacity_positive", "pon_ports", type_="check"
        )
    if "max_ont_capacity" in _columns("pon_ports"):
        op.drop_column("pon_ports", "max_ont_capacity")
