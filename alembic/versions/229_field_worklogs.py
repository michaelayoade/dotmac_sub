"""Add native field worklogs.

Revision ID: 229_field_worklogs
Revises: 228_field_work_order_notes
Create Date: 2026-07-09
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "229_field_worklogs"
down_revision = "228_field_work_order_notes"
branch_labels = None
depends_on = None

_TABLE = "field_worklogs"


def _has_table(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    if _has_table(_TABLE):
        return
    op.create_table(
        _TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "work_order_mirror_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("work_order_mirror.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("crm_work_order_id", sa.String(length=64), nullable=False),
        sa.Column(
            "author_technician_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("technician_profiles.id"),
            nullable=False,
        ),
        sa.Column("person_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "system_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("system_users.id"),
        ),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True)),
        sa.Column("minutes", sa.Integer(), nullable=False),
        sa.Column("notes", sa.Text()),
        sa.Column("client_ref", postgresql.UUID(as_uuid=True)),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_field_worklogs_mirror_start", _TABLE, ["work_order_mirror_id", "start_at"]
    )
    op.create_index(
        "ix_field_worklogs_crm_work_order_id", _TABLE, ["crm_work_order_id"]
    )
    op.create_index(
        "ix_field_worklogs_author_start", _TABLE, ["author_technician_id", "start_at"]
    )
    op.create_index("ix_field_worklogs_client_ref", _TABLE, ["client_ref"], unique=True)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    if not _has_table(_TABLE):
        return
    op.drop_index("ix_field_worklogs_client_ref", table_name=_TABLE)
    op.drop_index("ix_field_worklogs_author_start", table_name=_TABLE)
    op.drop_index("ix_field_worklogs_crm_work_order_id", table_name=_TABLE)
    op.drop_index("ix_field_worklogs_mirror_start", table_name=_TABLE)
    op.drop_table(_TABLE)
