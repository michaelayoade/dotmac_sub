"""scope ONT VLAN references to the assigned OLT

Revision ID: 049_scope_ont_vlan_refs_to_olt
Revises: 048_add_service_port_allocation_correlation
Create Date: 2026-04-22
"""

from __future__ import annotations

from sqlalchemy import inspect

from alembic import op

revision = "049_scope_ont_vlan_refs_to_olt"
down_revision = "048_add_service_port_allocation_correlation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    # The squashed initial migration (001) builds tables directly from the
    # current ORM models, which no longer carry the ``wan_vlan_id`` /
    # ``mgmt_vlan_id`` columns. On a fresh squash-built DB this migration
    # therefore has no per-column work to do, and the UPDATE-to-NULL +
    # FK-rewrite would fail with "column does not exist". Skip the column-
    # specific steps when the columns are absent.
    ont_columns = {c["name"] for c in inspector.get_columns("ont_units")}
    has_wan_vlan = "wan_vlan_id" in ont_columns
    has_mgmt_vlan = "mgmt_vlan_id" in ont_columns

    if has_wan_vlan:
        op.execute(
            """
            UPDATE ont_units AS ou
            SET wan_vlan_id = NULL
            FROM vlans AS v
            WHERE ou.wan_vlan_id = v.id
              AND (
                ou.olt_device_id IS NULL
                OR v.olt_device_id IS NULL
                OR ou.olt_device_id <> v.olt_device_id
              )
            """
        )
    if has_mgmt_vlan:
        op.execute(
            """
            UPDATE ont_units AS ou
            SET mgmt_vlan_id = NULL
            FROM vlans AS v
            WHERE ou.mgmt_vlan_id = v.id
              AND (
                ou.olt_device_id IS NULL
                OR v.olt_device_id IS NULL
                OR ou.olt_device_id <> v.olt_device_id
              )
            """
        )

    unique_constraints = {c["name"] for c in inspector.get_unique_constraints("vlans")}
    if "uq_vlans_olt_id" not in unique_constraints:
        op.create_unique_constraint(
            "uq_vlans_olt_id",
            "vlans",
            ["olt_device_id", "id"],
        )

    foreign_keys = {fk["name"] for fk in inspector.get_foreign_keys("ont_units")}
    if has_wan_vlan and "ont_units_wan_vlan_id_fkey" in foreign_keys:
        op.drop_constraint(
            "ont_units_wan_vlan_id_fkey", "ont_units", type_="foreignkey"
        )
    if has_mgmt_vlan and "ont_units_mgmt_vlan_id_fkey" in foreign_keys:
        op.drop_constraint(
            "ont_units_mgmt_vlan_id_fkey", "ont_units", type_="foreignkey"
        )

    foreign_keys = {fk["name"] for fk in inspector.get_foreign_keys("ont_units")}
    if has_wan_vlan and "fk_ont_units_wan_vlan_olt_scope" not in foreign_keys:
        op.create_foreign_key(
            "fk_ont_units_wan_vlan_olt_scope",
            "ont_units",
            "vlans",
            ["olt_device_id", "wan_vlan_id"],
            ["olt_device_id", "id"],
        )
    if has_mgmt_vlan and "fk_ont_units_mgmt_vlan_olt_scope" not in foreign_keys:
        op.create_foreign_key(
            "fk_ont_units_mgmt_vlan_olt_scope",
            "ont_units",
            "vlans",
            ["olt_device_id", "mgmt_vlan_id"],
            ["olt_device_id", "id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    foreign_keys = {fk["name"] for fk in inspector.get_foreign_keys("ont_units")}
    if "fk_ont_units_wan_vlan_olt_scope" in foreign_keys:
        op.drop_constraint(
            "fk_ont_units_wan_vlan_olt_scope", "ont_units", type_="foreignkey"
        )
    if "fk_ont_units_mgmt_vlan_olt_scope" in foreign_keys:
        op.drop_constraint(
            "fk_ont_units_mgmt_vlan_olt_scope", "ont_units", type_="foreignkey"
        )

    foreign_keys = {fk["name"] for fk in inspector.get_foreign_keys("ont_units")}
    if "ont_units_wan_vlan_id_fkey" not in foreign_keys:
        op.create_foreign_key(
            "ont_units_wan_vlan_id_fkey",
            "ont_units",
            "vlans",
            ["wan_vlan_id"],
            ["id"],
        )
    if "ont_units_mgmt_vlan_id_fkey" not in foreign_keys:
        op.create_foreign_key(
            "ont_units_mgmt_vlan_id_fkey",
            "ont_units",
            "vlans",
            ["mgmt_vlan_id"],
            ["id"],
        )

    unique_constraints = {c["name"] for c in inspector.get_unique_constraints("vlans")}
    if "uq_vlans_olt_id" in unique_constraints:
        op.drop_constraint("uq_vlans_olt_id", "vlans", type_="unique")
