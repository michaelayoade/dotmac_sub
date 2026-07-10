"""Add native field expense requests.

Revision ID: 235_field_expense_requests
Revises: 234_field_material_requests
Create Date: 2026-07-09
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "235_field_expense_requests"
down_revision = "234_field_material_requests"
branch_labels = None
depends_on = None

_REQUESTS = "field_expense_requests"
_ITEMS = "field_expense_request_items"


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
            sa.Column("crm_expense_request_id", sa.String(length=64), unique=True),
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
            sa.Column("purpose", sa.String(length=500), nullable=False),
            sa.Column("expense_date", sa.Date()),
            sa.Column("currency", sa.String(length=3), nullable=False),
            sa.Column("notes", sa.Text()),
            sa.Column("rejection_reason", sa.String(length=500)),
            sa.Column("erp_expense_claim_id", sa.String(length=120)),
            sa.Column("erp_claim_number", sa.String(length=60)),
            sa.Column("erp_claim_status", sa.String(length=40)),
            sa.Column("client_ref", postgresql.UUID(as_uuid=True)),
            sa.Column("metadata", sa.JSON()),
            sa.Column("submitted_at", sa.DateTime(timezone=True)),
            sa.Column("approved_at", sa.DateTime(timezone=True)),
            sa.Column("rejected_at", sa.DateTime(timezone=True)),
            sa.Column("paid_at", sa.DateTime(timezone=True)),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "status IN ('draft', 'submitted', 'approved', 'rejected', 'paid', 'canceled')",
                name="ck_field_expense_requests_status",
            ),
        )
        op.create_index(
            "ix_field_expense_requests_mirror",
            _REQUESTS,
            ["work_order_mirror_id", "created_at"],
        )
        op.create_index(
            "ix_field_expense_requests_crm_work_order_id",
            _REQUESTS,
            ["crm_work_order_id"],
        )
        op.create_index(
            "ix_field_expense_requests_requested_by",
            _REQUESTS,
            ["requested_by_technician_id"],
        )
        op.create_index("ix_field_expense_requests_status", _REQUESTS, ["status"])
        op.create_index(
            "ix_field_expense_requests_client_ref",
            _REQUESTS,
            ["client_ref"],
            unique=True,
        )

    if not _has_table(_ITEMS):
        op.create_table(
            _ITEMS,
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "expense_request_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("field_expense_requests.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("category_code", sa.String(length=30), nullable=False),
            sa.Column("category_name", sa.String(length=120)),
            sa.Column("description", sa.String(length=500), nullable=False),
            sa.Column("amount", sa.Numeric(14, 2), nullable=False),
            sa.Column("expense_date", sa.Date()),
            sa.Column("vendor_name", sa.String(length=200)),
            sa.Column("receipt_url", sa.String(length=500)),
            sa.Column(
                "receipt_attachment_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("field_attachments.id", ondelete="SET NULL"),
            ),
            sa.Column("notes", sa.Text()),
            sa.Column("metadata", sa.JSON()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "amount > 0",
                name="ck_field_expense_request_items_amount_positive",
            ),
        )
        op.create_index(
            "ix_field_expense_request_items_request",
            _ITEMS,
            ["expense_request_id", "created_at"],
        )
        op.create_index(
            "ix_field_expense_request_items_category",
            _ITEMS,
            ["category_code"],
        )
        op.create_index(
            "ix_field_expense_request_items_receipt_attachment",
            _ITEMS,
            ["receipt_attachment_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    if _has_table(_ITEMS):
        op.drop_index(
            "ix_field_expense_request_items_receipt_attachment", table_name=_ITEMS
        )
        op.drop_index("ix_field_expense_request_items_category", table_name=_ITEMS)
        op.drop_index("ix_field_expense_request_items_request", table_name=_ITEMS)
        op.drop_table(_ITEMS)
    if _has_table(_REQUESTS):
        op.drop_index("ix_field_expense_requests_client_ref", table_name=_REQUESTS)
        op.drop_index("ix_field_expense_requests_status", table_name=_REQUESTS)
        op.drop_index("ix_field_expense_requests_requested_by", table_name=_REQUESTS)
        op.drop_index(
            "ix_field_expense_requests_crm_work_order_id", table_name=_REQUESTS
        )
        op.drop_index("ix_field_expense_requests_mirror", table_name=_REQUESTS)
        op.drop_table(_REQUESTS)
