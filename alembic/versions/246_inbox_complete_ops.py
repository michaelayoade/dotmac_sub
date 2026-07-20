"""Add complete inbox operational primitives.

Revision ID: 246_inbox_complete_ops
Revises: 245_inbox_parity_workflows
Create Date: 2026-07-10
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "246_inbox_complete_ops"
down_revision = "245_inbox_parity_workflows"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def _has_index(table_name: str, index_name: str) -> bool:
    return index_name in {
        index["name"] for index in inspect(op.get_bind()).get_indexes(table_name)
    }


def _json_type() -> sa.types.TypeEngine:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        return postgresql.JSONB(astext_type=sa.Text())
    return sa.JSON()


def _uuid_type() -> sa.types.TypeEngine:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        return postgresql.UUID(as_uuid=True)
    return sa.String(length=36)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return

    uuid_type = _uuid_type()
    json_type = _json_type()

    if not _has_table("inbox_media_assets"):
        op.create_table(
            "inbox_media_assets",
            sa.Column("id", uuid_type, primary_key=True),
            sa.Column("conversation_id", uuid_type, nullable=False),
            sa.Column("message_id", uuid_type),
            sa.Column("channel_type", sa.String(length=40), nullable=False),
            sa.Column("direction", sa.String(length=40), nullable=False),
            sa.Column("provider", sa.String(length=80)),
            sa.Column("provider_media_id", sa.String(length=255)),
            sa.Column("asset_type", sa.String(length=40), nullable=False),
            sa.Column("file_name", sa.String(length=255)),
            sa.Column("mime_type", sa.String(length=160)),
            sa.Column("file_size", sa.Integer()),
            sa.Column("caption", sa.Text()),
            sa.Column("source_url", sa.Text()),
            sa.Column("storage_url", sa.Text()),
            sa.Column("checksum_sha256", sa.String(length=64)),
            sa.Column(
                "download_status",
                sa.String(length=40),
                nullable=False,
                server_default="metadata_only",
            ),
            sa.Column("download_error", sa.Text()),
            sa.Column("metadata", json_type),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
            sa.ForeignKeyConstraint(["conversation_id"], ["inbox_conversations.id"]),
            sa.ForeignKeyConstraint(["message_id"], ["inbox_messages.id"]),
        )
    for index_name, columns in {
        "ix_inbox_media_assets_conversation": ["conversation_id", "created_at"],
        "ix_inbox_media_assets_message": ["message_id"],
        "ix_inbox_media_assets_provider": ["provider", "provider_media_id"],
        "ix_inbox_media_assets_download_status": ["download_status"],
    }.items():
        if _has_table("inbox_media_assets") and not _has_index(
            "inbox_media_assets", index_name
        ):
            op.create_index(index_name, "inbox_media_assets", columns)

    if not _has_table("inbox_comments"):
        op.create_table(
            "inbox_comments",
            sa.Column("id", uuid_type, primary_key=True),
            sa.Column("conversation_id", uuid_type, nullable=False),
            sa.Column("message_id", uuid_type),
            sa.Column("author_person_id", uuid_type),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column(
                "visibility",
                sa.String(length=40),
                nullable=False,
                server_default="internal",
            ),
            sa.Column(
                "is_resolved",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
            sa.Column("resolved_by_person_id", uuid_type),
            sa.Column("resolved_at", sa.DateTime(timezone=True)),
            sa.Column("metadata", json_type),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
            sa.ForeignKeyConstraint(["conversation_id"], ["inbox_conversations.id"]),
            sa.ForeignKeyConstraint(["message_id"], ["inbox_messages.id"]),
        )
    for index_name, columns in {
        "ix_inbox_comments_conversation": ["conversation_id", "created_at"],
        "ix_inbox_comments_message": ["message_id"],
        "ix_inbox_comments_author": ["author_person_id", "created_at"],
        "ix_inbox_comments_resolved": ["is_resolved", "created_at"],
    }.items():
        if _has_table("inbox_comments") and not _has_index(
            "inbox_comments", index_name
        ):
            op.create_index(index_name, "inbox_comments", columns)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    if _has_table("inbox_comments"):
        op.drop_table("inbox_comments")
    if _has_table("inbox_media_assets"):
        op.drop_table("inbox_media_assets")
