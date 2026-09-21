"""Add auditable approved amounts to field expense requests.

Revision ID: 617_expense_approval_adjustments
Revises: 616_fiber_acquisition_attribution
Create Date: 2026-09-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "617_expense_approval_adjustments"
down_revision: str | None = "616_fiber_acquisition_attribution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if "permissions" in sa.inspect(bind).get_table_names():
        bind.execute(
            sa.text(
                "UPDATE permissions SET description = :description WHERE key = :key"
            ),
            {
                "key": "operations:expense_request:write",
                "description": (
                    "Adjust amounts and approve or reject field expense requests"
                ),
            },
        )
    op.add_column(
        "field_expense_requests",
        sa.Column(
            "approved_by_system_user_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.add_column(
        "field_expense_requests",
        sa.Column(
            "approval_decision_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.add_column(
        "field_expense_requests",
        sa.Column("approval_adjustment_reason", sa.String(length=500), nullable=True),
    )
    op.add_column(
        "field_expense_requests",
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_foreign_key(
        "fk_field_expense_requests_approved_by_system_user",
        "field_expense_requests",
        "system_users",
        ["approved_by_system_user_id"],
        ["id"],
    )
    op.create_index(
        "ix_field_expense_requests_approved_by",
        "field_expense_requests",
        ["approved_by_system_user_id"],
    )
    op.create_index(
        "ux_field_expense_requests_approval_decision",
        "field_expense_requests",
        ["approval_decision_id"],
        unique=True,
    )

    op.add_column(
        "field_expense_request_items",
        sa.Column("approved_amount", sa.Numeric(14, 2), nullable=True),
    )
    op.create_check_constraint(
        "ck_field_expense_request_items_approved_amount_positive",
        "field_expense_request_items",
        "approved_amount IS NULL OR approved_amount > 0",
    )
    op.execute(
        """
        UPDATE field_expense_request_items AS item
        SET approved_amount = item.amount
        FROM field_expense_requests AS request
        WHERE request.id = item.expense_request_id
          AND request.status IN ('approved', 'paid')
          AND item.approved_amount IS NULL
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    if "permissions" in sa.inspect(bind).get_table_names():
        bind.execute(
            sa.text(
                "UPDATE permissions SET description = :description WHERE key = :key"
            ),
            {
                "key": "operations:expense_request:write",
                "description": "Approve or reject field expense requests",
            },
        )
    op.drop_constraint(
        "ck_field_expense_request_items_approved_amount_positive",
        "field_expense_request_items",
        type_="check",
    )
    op.drop_column("field_expense_request_items", "approved_amount")

    op.drop_index(
        "ux_field_expense_requests_approval_decision",
        table_name="field_expense_requests",
    )
    op.drop_index(
        "ix_field_expense_requests_approved_by",
        table_name="field_expense_requests",
    )
    op.drop_constraint(
        "fk_field_expense_requests_approved_by_system_user",
        "field_expense_requests",
        type_="foreignkey",
    )
    op.drop_column("field_expense_requests", "revision")
    op.drop_column("field_expense_requests", "approval_adjustment_reason")
    op.drop_column("field_expense_requests", "approval_decision_id")
    op.drop_column("field_expense_requests", "approved_by_system_user_id")
