"""Add inbox parity workflow fields.

Revision ID: 245_inbox_parity_workflows
Revises: 244_inbox_completion_templates
Create Date: 2026-07-10
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "245_inbox_parity_workflows"
down_revision = "244_inbox_completion_templates"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def _columns(table_name: str) -> set[str]:
    return {column["name"] for column in inspect(op.get_bind()).get_columns(table_name)}


def _has_index(table_name: str, index_name: str) -> bool:
    return index_name in {
        index["name"] for index in inspect(op.get_bind()).get_indexes(table_name)
    }


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    if _has_table("inbox_conversations"):
        columns = _columns("inbox_conversations")
        if "priority" not in columns:
            op.add_column(
                "inbox_conversations",
                sa.Column(
                    "priority", sa.Integer(), nullable=False, server_default="100"
                ),
            )
        if "is_muted" not in columns:
            op.add_column(
                "inbox_conversations",
                sa.Column(
                    "is_muted", sa.Boolean(), nullable=False, server_default=sa.false()
                ),
            )
        if "snoozed_until" not in columns:
            op.add_column(
                "inbox_conversations",
                sa.Column("snoozed_until", sa.DateTime(timezone=True)),
            )
        if not _has_index("inbox_conversations", "ix_inbox_conversations_priority"):
            op.create_index(
                "ix_inbox_conversations_priority",
                "inbox_conversations",
                ["priority", "last_message_at"],
            )
        if not _has_index("inbox_conversations", "ix_inbox_conversations_snoozed"):
            op.create_index(
                "ix_inbox_conversations_snoozed",
                "inbox_conversations",
                ["snoozed_until"],
            )

    if not _has_table("inbox_saved_filters"):
        op.create_table(
            "inbox_saved_filters",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("owner_person_id", postgresql.UUID(as_uuid=True)),
            sa.Column(
                "filter_payload",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
            ),
            sa.Column(
                "is_shared", sa.Boolean(), nullable=False, server_default=sa.false()
            ),
            sa.Column(
                "is_active", sa.Boolean(), nullable=False, server_default=sa.true()
            ),
            sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text())),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
        )
        op.create_index(
            "ix_inbox_saved_filters_owner_active",
            "inbox_saved_filters",
            ["owner_person_id", "is_active"],
        )
        op.create_index(
            "ix_inbox_saved_filters_shared_active",
            "inbox_saved_filters",
            ["is_shared", "is_active"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    if _has_table("inbox_saved_filters"):
        op.drop_table("inbox_saved_filters")
    if _has_table("inbox_conversations"):
        columns = _columns("inbox_conversations")
        if "snoozed_until" in columns:
            op.drop_column("inbox_conversations", "snoozed_until")
        if "is_muted" in columns:
            op.drop_column("inbox_conversations", "is_muted")
        if "priority" in columns:
            op.drop_column("inbox_conversations", "priority")
