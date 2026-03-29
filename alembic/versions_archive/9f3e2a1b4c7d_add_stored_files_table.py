"""add stored_files table for private object storage

Revision ID: 9f3e2a1b4c7d
Revises: 7a1c9e2d4f55
Create Date: 2026-02-24 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9f3e2a1b4c7d"
down_revision: str | None = "7a1c9e2d4f55"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "stored_files",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("entity_type", sa.String(length=100), nullable=False),
        sa.Column("entity_id", sa.String(length=100), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("storage_key_or_relative_path", sa.String(length=1024), nullable=False),
        sa.Column("legacy_local_path", sa.String(length=1024), nullable=True),
        sa.Column("file_size", sa.Integer(), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=True),
        sa.Column("checksum", sa.String(length=64), nullable=True),
        sa.Column("storage_provider", sa.String(length=20), nullable=False),
        sa.Column("uploaded_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["uploaded_by"], ["subscribers.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_stored_files_entity", "stored_files", ["entity_type", "entity_id"], unique=False
    )
    op.create_index(
        "ix_stored_files_org_active",
        "stored_files",
        ["organization_id", "is_deleted"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_stored_files_org_active", table_name="stored_files")
    op.drop_index("ix_stored_files_entity", table_name="stored_files")
    op.drop_table("stored_files")
