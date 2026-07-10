"""Add inbox labels and reply macros.

Revision ID: 243_inbox_labels_macros
Revises: 242_inbox_contact_links
Create Date: 2026-07-10
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "243_inbox_labels_macros"
down_revision = "242_inbox_contact_links"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    if not _has_table("inbox_labels"):
        op.create_table(
            "inbox_labels",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("name", sa.String(length=80), nullable=False),
            sa.Column("slug", sa.String(length=100), nullable=False),
            sa.Column("color", sa.String(length=24)),
            sa.Column("description", sa.String(length=255)),
            sa.Column(
                "is_active", sa.Boolean(), nullable=False, server_default=sa.true()
            ),
            sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text())),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
            sa.UniqueConstraint("slug", name="uq_inbox_labels_slug"),
        )
        op.create_index(
            "ix_inbox_labels_active",
            "inbox_labels",
            ["is_active", "name"],
        )

    if not _has_table("inbox_conversation_labels"):
        op.create_table(
            "inbox_conversation_labels",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("label_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("applied_by_person_id", postgresql.UUID(as_uuid=True)),
            sa.Column(
                "is_active", sa.Boolean(), nullable=False, server_default=sa.true()
            ),
            sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text())),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
            sa.ForeignKeyConstraint(["conversation_id"], ["inbox_conversations.id"]),
            sa.ForeignKeyConstraint(["label_id"], ["inbox_labels.id"]),
        )
        op.create_index(
            "ix_inbox_conversation_labels_conversation",
            "inbox_conversation_labels",
            ["conversation_id", "is_active"],
        )
        op.create_index(
            "ix_inbox_conversation_labels_label",
            "inbox_conversation_labels",
            ["label_id", "is_active"],
        )
        op.create_index(
            "uq_inbox_conversation_labels_active",
            "inbox_conversation_labels",
            ["conversation_id", "label_id"],
            unique=True,
            postgresql_where=sa.text("is_active IS TRUE"),
        )

    if not _has_table("inbox_reply_macros"):
        op.create_table(
            "inbox_reply_macros",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("description", sa.Text()),
            sa.Column("body_text", sa.Text(), nullable=False),
            sa.Column("visibility", sa.String(length=40), nullable=False),
            sa.Column("created_by_person_id", postgresql.UUID(as_uuid=True)),
            sa.Column("actions", postgresql.JSONB(astext_type=sa.Text())),
            sa.Column(
                "execution_count", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column(
                "is_active", sa.Boolean(), nullable=False, server_default=sa.true()
            ),
            sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text())),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
        )
        op.create_index(
            "ix_inbox_reply_macros_active",
            "inbox_reply_macros",
            ["is_active", "name"],
        )
        op.create_index(
            "ix_inbox_reply_macros_creator",
            "inbox_reply_macros",
            ["created_by_person_id", "is_active"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    for table_name in (
        "inbox_reply_macros",
        "inbox_conversation_labels",
        "inbox_labels",
    ):
        if _has_table(table_name):
            op.drop_table(table_name)
