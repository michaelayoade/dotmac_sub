"""Track ERP department-managed service-team membership.

Revision ID: 563_erp_department_service_team_membership
Revises: 560_ncc_report_permissions
Create Date: 2026-08-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "563_erp_department_service_team_membership"
down_revision: str | None = "560_ncc_report_permissions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_NAME = "service_team_department_membership_sources"


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if TABLE_NAME in tables:
        return
    op.create_table(
        TABLE_NAME,
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True
        ),
        sa.Column("provider", sa.String(length=80), nullable=False),
        sa.Column("account_scope", sa.String(length=120), nullable=False),
        sa.Column("external_employee_id", sa.String(length=200), nullable=False),
        sa.Column("employee_code", sa.String(length=80)),
        sa.Column("external_department_id", sa.String(length=200), nullable=False),
        sa.Column("department_code", sa.String(length=80)),
        sa.Column("department_name", sa.String(length=200)),
        sa.Column(
            "system_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("system_users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "person_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("parties.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "team_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("service_teams.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "member_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("service_team_members.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "provider",
            "account_scope",
            "external_employee_id",
            name="uq_service_team_department_membership_source_employee",
        ),
    )
    op.create_index(
        "ix_service_team_department_membership_source_member",
        TABLE_NAME,
        ["member_id"],
    )
    op.create_index(
        "ix_service_team_department_membership_source_team_active",
        TABLE_NAME,
        ["team_id", "is_active"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if TABLE_NAME not in tables:
        return
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes(TABLE_NAME)}
    for name in (
        "ix_service_team_department_membership_source_team_active",
        "ix_service_team_department_membership_source_member",
    ):
        if name in indexes:
            op.drop_index(name, table_name=TABLE_NAME)
    op.drop_table(TABLE_NAME)
