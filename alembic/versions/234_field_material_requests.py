"""Add native field material requests.

Revision ID: 234_field_material_requests
Revises: 233_field_materials
Create Date: 2026-07-09
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "234_field_material_requests"
down_revision = "233_field_materials"
branch_labels = None
depends_on = None

_REQUESTS = "field_material_requests"
_ITEMS = "field_material_request_items"


def _has_table(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    if not _has_table(_REQUESTS):
        op.create_table(
            _REQUESTS,
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "work_order_mirror_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("work_order_mirror.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("crm_work_order_id", sa.String(length=64), nullable=False),
            sa.Column("crm_material_request_id", sa.String(length=64), unique=True),
            sa.Column(
                "requested_by_technician_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("technician_profiles.id"),
                nullable=False,
            ),
            sa.Column(
                "requested_by_person_id",
                postgresql.UUID(as_uuid=True),
                nullable=False,
            ),
            sa.Column(
                "requested_by_system_user_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("system_users.id"),
            ),
            sa.Column("status", sa.String(length=30), nullable=False),
            sa.Column("priority", sa.String(length=20), nullable=False),
            sa.Column("notes", sa.Text()),
            sa.Column("metadata", sa.JSON()),
            sa.Column("submitted_at", sa.DateTime(timezone=True)),
            sa.Column("approved_at", sa.DateTime(timezone=True)),
            sa.Column("rejected_at", sa.DateTime(timezone=True)),
            sa.Column("fulfilled_at", sa.DateTime(timezone=True)),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "status IN ('draft', 'submitted', 'approved', 'rejected', 'issued', "
                "'fulfilled', 'canceled')",
                name="ck_field_material_requests_status",
            ),
            sa.CheckConstraint(
                "priority IN ('low', 'medium', 'high', 'urgent')",
                name="ck_field_material_requests_priority",
            ),
        )
        op.create_index(
            "ix_field_material_requests_mirror",
            _REQUESTS,
            ["work_order_mirror_id", "created_at"],
        )
        op.create_index(
            "ix_field_material_requests_crm_work_order_id",
            _REQUESTS,
            ["crm_work_order_id"],
        )
        op.create_index("ix_field_material_requests_status", _REQUESTS, ["status"])
        op.create_index(
            "ix_field_material_requests_requested_by",
            _REQUESTS,
            ["requested_by_technician_id"],
        )

    if not _has_table(_ITEMS):
        op.create_table(
            _ITEMS,
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "material_request_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("field_material_requests.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "item_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("field_inventory_items.id"),
                nullable=False,
            ),
            sa.Column("quantity", sa.Integer(), nullable=False),
            sa.Column("notes", sa.Text()),
            sa.Column("metadata", sa.JSON()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "quantity > 0",
                name="ck_field_material_request_items_quantity_positive",
            ),
        )
        op.create_index(
            "ix_field_material_request_items_request",
            _ITEMS,
            ["material_request_id", "created_at"],
        )
        op.create_index("ix_field_material_request_items_item", _ITEMS, ["item_id"])


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    if _has_table(_ITEMS):
        op.drop_index("ix_field_material_request_items_item", table_name=_ITEMS)
        op.drop_index("ix_field_material_request_items_request", table_name=_ITEMS)
        op.drop_table(_ITEMS)
    if _has_table(_REQUESTS):
        op.drop_index("ix_field_material_requests_requested_by", table_name=_REQUESTS)
        op.drop_index("ix_field_material_requests_status", table_name=_REQUESTS)
        op.drop_index(
            "ix_field_material_requests_crm_work_order_id", table_name=_REQUESTS
        )
        op.drop_index("ix_field_material_requests_mirror", table_name=_REQUESTS)
        op.drop_table(_REQUESTS)
