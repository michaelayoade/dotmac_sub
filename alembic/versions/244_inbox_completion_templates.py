"""Add inbox message templates.

Revision ID: 244_inbox_completion_templates
Revises: 243_inbox_labels_macros
Create Date: 2026-07-10
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "244_inbox_completion_templates"
down_revision = "243_inbox_labels_macros"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    if _has_table("inbox_message_templates"):
        return
    op.create_table(
        "inbox_message_templates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("channel_type", sa.String(length=40), nullable=False),
        sa.Column("subject", sa.String(length=200)),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("body_html", sa.Text()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_inbox_message_templates_channel_active",
        "inbox_message_templates",
        ["channel_type", "is_active"],
    )
    op.create_index(
        "ix_inbox_message_templates_name",
        "inbox_message_templates",
        ["name"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    if _has_table("inbox_message_templates"):
        op.drop_table("inbox_message_templates")
