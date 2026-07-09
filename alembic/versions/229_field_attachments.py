"""Add native field attachments.

Revision ID: 229_field_attachments
Revises: 228_field_worklogs
Create Date: 2026-07-09
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "229_field_attachments"
down_revision = "228_field_worklogs"
branch_labels = None
depends_on = None

_TABLE = "field_attachments"


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
            "note_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("field_work_order_notes.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "stored_file_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("stored_files.id"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("file_name", sa.String(length=255), nullable=False),
        sa.Column("mime_type", sa.String(length=255), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("latitude", sa.Float()),
        sa.Column("longitude", sa.Float()),
        sa.Column("captured_at", sa.DateTime(timezone=True)),
        sa.Column("signer_name", sa.String(length=160)),
        sa.Column(
            "uploaded_by_technician_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("technician_profiles.id"),
            nullable=False,
        ),
        sa.Column(
            "uploaded_by_person_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "uploaded_by_system_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("system_users.id"),
        ),
        sa.Column("client_ref", postgresql.UUID(as_uuid=True)),
        sa.Column("asset_type", sa.String(length=60)),
        sa.Column("asset_id", postgresql.UUID(as_uuid=True)),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_field_attachments_mirror_created",
        _TABLE,
        ["work_order_mirror_id", "created_at"],
    )
    op.create_index(
        "ix_field_attachments_crm_work_order_id", _TABLE, ["crm_work_order_id"]
    )
    op.create_index("ix_field_attachments_note_id", _TABLE, ["note_id"])
    op.create_index(
        "ix_field_attachments_client_ref", _TABLE, ["client_ref"], unique=True
    )
    op.create_index("ix_field_attachments_asset", _TABLE, ["asset_type", "asset_id"])


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    if not _has_table(_TABLE):
        return
    op.drop_index("ix_field_attachments_asset", table_name=_TABLE)
    op.drop_index("ix_field_attachments_client_ref", table_name=_TABLE)
    op.drop_index("ix_field_attachments_note_id", table_name=_TABLE)
    op.drop_index("ix_field_attachments_crm_work_order_id", table_name=_TABLE)
    op.drop_index("ix_field_attachments_mirror_created", table_name=_TABLE)
    op.drop_table(_TABLE)
