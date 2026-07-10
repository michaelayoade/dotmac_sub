"""Add native field material allocation tables.

Revision ID: 233_field_materials
Revises: 232_field_ont_assignment_work_order
Create Date: 2026-07-09
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "233_field_materials"
down_revision = "232_field_ont_assignment_work_order"
branch_labels = None
depends_on = None

_ITEMS = "field_inventory_items"
_MATERIALS = "field_work_order_materials"


def _has_table(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    if not _has_table(_ITEMS):
        op.create_table(
            _ITEMS,
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("crm_item_id", sa.String(length=64), unique=True),
            sa.Column("sku", sa.String(length=80)),
            sa.Column("name", sa.String(length=160), nullable=False),
            sa.Column("unit", sa.String(length=40)),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("metadata", sa.JSON()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_field_inventory_items_sku", _ITEMS, ["sku"])
        op.create_index("ix_field_inventory_items_name", _ITEMS, ["name"])

    if not _has_table(_MATERIALS):
        op.create_table(
            _MATERIALS,
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "work_order_mirror_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("work_order_mirror.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("crm_work_order_id", sa.String(length=64), nullable=False),
            sa.Column("crm_material_id", sa.String(length=64)),
            sa.Column(
                "item_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("field_inventory_items.id"),
                nullable=False,
            ),
            sa.Column("allocated_quantity", sa.Integer(), nullable=False),
            sa.Column("consumed_quantity", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=30), nullable=False),
            sa.Column("notes", sa.Text()),
            sa.Column("metadata", sa.JSON()),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "allocated_quantity >= 0",
                name="ck_field_work_order_materials_allocated_nonnegative",
            ),
            sa.CheckConstraint(
                "consumed_quantity >= 0",
                name="ck_field_work_order_materials_consumed_nonnegative",
            ),
            sa.CheckConstraint(
                "consumed_quantity <= allocated_quantity",
                name="ck_field_work_order_materials_consumed_lte_allocated",
            ),
            sa.CheckConstraint(
                "status IN ('required', 'reserved', 'used')",
                name="ck_field_work_order_materials_status",
            ),
        )
        op.create_index(
            "ix_field_work_order_materials_mirror",
            _MATERIALS,
            ["work_order_mirror_id", "created_at"],
        )
        op.create_index(
            "ix_field_work_order_materials_crm_work_order_id",
            _MATERIALS,
            ["crm_work_order_id"],
        )
        op.create_index("ix_field_work_order_materials_item", _MATERIALS, ["item_id"])
        op.create_index(
            "ix_field_work_order_materials_crm_material_id",
            _MATERIALS,
            ["crm_material_id"],
        )
        op.create_index(
            "ix_field_work_order_materials_status",
            _MATERIALS,
            ["status"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    if _has_table(_MATERIALS):
        op.drop_index("ix_field_work_order_materials_status", table_name=_MATERIALS)
        op.drop_index(
            "ix_field_work_order_materials_crm_material_id", table_name=_MATERIALS
        )
        op.drop_index("ix_field_work_order_materials_item", table_name=_MATERIALS)
        op.drop_index(
            "ix_field_work_order_materials_crm_work_order_id", table_name=_MATERIALS
        )
        op.drop_index("ix_field_work_order_materials_mirror", table_name=_MATERIALS)
        op.drop_table(_MATERIALS)
    if _has_table(_ITEMS):
        op.drop_index("ix_field_inventory_items_name", table_name=_ITEMS)
        op.drop_index("ix_field_inventory_items_sku", table_name=_ITEMS)
        op.drop_table(_ITEMS)
