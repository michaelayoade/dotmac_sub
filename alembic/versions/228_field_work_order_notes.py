"""Add native field work-order notes.

Revision ID: 228_field_work_order_notes
Revises: 227_field_location_tracking
Create Date: 2026-07-09
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "228_field_work_order_notes"
down_revision = "227_field_location_tracking"
branch_labels = None
depends_on = None

_TABLE = "field_work_order_notes"


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
        sa.Column("author_person_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "author_system_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("system_users.id"),
        ),
        sa.Column("author_name", sa.String(length=160)),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("is_internal", sa.Boolean(), nullable=False),
        sa.Column("attachments", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_field_work_order_notes_mirror_created",
        _TABLE,
        ["work_order_mirror_id", "created_at"],
    )
    op.create_index(
        "ix_field_work_order_notes_crm_work_order_id",
        _TABLE,
        ["crm_work_order_id"],
    )
    op.create_index(
        "ix_field_work_order_notes_author_technician",
        _TABLE,
        ["author_technician_id"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    if not _has_table(_TABLE):
        return
    op.drop_index("ix_field_work_order_notes_author_technician", table_name=_TABLE)
    op.drop_index("ix_field_work_order_notes_crm_work_order_id", table_name=_TABLE)
    op.drop_index("ix_field_work_order_notes_mirror_created", table_name=_TABLE)
    op.drop_table(_TABLE)
