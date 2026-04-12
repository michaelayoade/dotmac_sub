"""scope vlan uniqueness to olt

Revision ID: 016_scope_vlan_uniqueness_to_olt
Revises: 015_add_olt_scope_to_ont_provisioning_profiles
Create Date: 2026-04-12
"""

from __future__ import annotations

from sqlalchemy import inspect

from alembic import op

revision = "016_scope_vlan_uniqueness_to_olt"
down_revision = "015_add_olt_scope_to_ont_provisioning_profiles"
branch_labels = None
depends_on = None


def _unique_constraints(table_name: str) -> set[str]:
    inspector = inspect(op.get_bind())
    return {constraint["name"] for constraint in inspector.get_unique_constraints(table_name)}


def upgrade() -> None:
    constraints = _unique_constraints("vlans")
    if "uq_vlans_region_tag" in constraints:
        op.drop_constraint("uq_vlans_region_tag", "vlans", type_="unique")
    if "uq_vlans_region_olt_tag" not in constraints:
        op.create_unique_constraint(
            "uq_vlans_region_olt_tag",
            "vlans",
            ["region_id", "olt_device_id", "tag"],
        )


def downgrade() -> None:
    constraints = _unique_constraints("vlans")
    if "uq_vlans_region_olt_tag" in constraints:
        op.drop_constraint("uq_vlans_region_olt_tag", "vlans", type_="unique")
    if "uq_vlans_region_tag" not in constraints:
        op.create_unique_constraint(
            "uq_vlans_region_tag",
            "vlans",
            ["region_id", "tag"],
        )
