"""Add ERP staff access restriction projections.

Revision ID: 572_erp_staff_access_restrictions
Revises: 571_seed_workqueue_audience_permissions
Create Date: 2026-09-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "572_erp_staff_access_restrictions"
down_revision: str | None = "571_seed_workqueue_audience_permissions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "erp_staff_leave_restrictions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("source_system", sa.String(length=80), nullable=False),
        sa.Column("restriction_id", sa.String(length=200), nullable=False),
        sa.Column("erp_employee_id", sa.String(length=200), nullable=False),
        sa.Column("system_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_event_id", sa.String(length=240), nullable=False),
        sa.Column("last_delivery_id", sa.String(length=240), nullable=True),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'cancelled', 'revoked')",
            name="ck_erp_staff_leave_restrictions_status",
        ),
        sa.CheckConstraint(
            "version >= 1", name="ck_erp_staff_leave_restrictions_version"
        ),
        sa.ForeignKeyConstraint(
            ["system_user_id"],
            ["system_users.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_system",
            "restriction_id",
            name="uq_erp_staff_leave_restriction_identity",
        ),
    )
    op.create_index(
        "ix_erp_staff_leave_restrictions_active_user",
        "erp_staff_leave_restrictions",
        ["system_user_id", "status", "effective_from", "effective_until"],
    )

    op.create_table(
        "erp_staff_account_status_projections",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_system", sa.String(length=80), nullable=False),
        sa.Column("erp_employee_id", sa.String(length=200), nullable=False),
        sa.Column("system_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("desired_status", sa.String(length=24), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.String(length=240), nullable=True),
        sa.Column("last_event_id", sa.String(length=240), nullable=False),
        sa.Column("last_delivery_id", sa.String(length=240), nullable=True),
        sa.Column("erp_inactive_applied", sa.Boolean(), nullable=False),
        sa.Column("erp_inactive_applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "desired_status IN ('active', 'inactive')",
            name="ck_erp_staff_account_status_desired",
        ),
        sa.CheckConstraint("version >= 1", name="ck_erp_staff_account_status_version"),
        sa.ForeignKeyConstraint(
            ["system_user_id"],
            ["system_users.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_system",
            "erp_employee_id",
            name="uq_erp_staff_account_status_employee",
        ),
    )
    op.create_index(
        "ix_erp_staff_account_status_user",
        "erp_staff_account_status_projections",
        ["system_user_id", "desired_status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_erp_staff_account_status_user",
        table_name="erp_staff_account_status_projections",
    )
    op.drop_table("erp_staff_account_status_projections")
    op.drop_index(
        "ix_erp_staff_leave_restrictions_active_user",
        table_name="erp_staff_leave_restrictions",
    )
    op.drop_table("erp_staff_leave_restrictions")
