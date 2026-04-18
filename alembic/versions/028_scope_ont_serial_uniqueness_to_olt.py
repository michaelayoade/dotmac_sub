"""scope ont serial uniqueness to olt

Revision ID: 028_scope_ont_serial_uniqueness_to_olt
Revises: 026_add_ont_wan_service_instances
Create Date: 2026-04-17
"""

from __future__ import annotations

from sqlalchemy import inspect

from alembic import op

revision = "028_scope_ont_serial_uniqueness_to_olt"
down_revision = "026_add_ont_wan_service_instances"
branch_labels = None
depends_on = None


def _unique_constraints(table_name: str) -> set[str]:
    inspector = inspect(op.get_bind())
    return {
        constraint["name"]
        for constraint in inspector.get_unique_constraints(table_name)
        if constraint.get("name")
    }


def upgrade() -> None:
    constraints = _unique_constraints("ont_units")
    if "uq_ont_units_serial_number" in constraints:
        op.drop_constraint("uq_ont_units_serial_number", "ont_units", type_="unique")
    if "uq_ont_units_olt_serial_number" not in constraints:
        op.create_unique_constraint(
            "uq_ont_units_olt_serial_number",
            "ont_units",
            ["olt_device_id", "serial_number"],
        )


def downgrade() -> None:
    constraints = _unique_constraints("ont_units")
    if "uq_ont_units_olt_serial_number" in constraints:
        op.drop_constraint(
            "uq_ont_units_olt_serial_number", "ont_units", type_="unique"
        )
    if "uq_ont_units_serial_number" not in constraints:
        op.create_unique_constraint(
            "uq_ont_units_serial_number",
            "ont_units",
            ["serial_number"],
        )
