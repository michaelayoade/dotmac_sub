"""Add inbox agent availability.

Revision ID: 240_inbox_agent_availability
Revises: 239_team_inbox_routing_foundation
Create Date: 2026-07-10
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "240_inbox_agent_availability"
down_revision = "239_team_inbox_routing_foundation"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    if not _has_table("inbox_agent_presence"):
        op.create_table(
            "inbox_agent_presence",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("person_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("status", sa.String(length=40), nullable=False),
            sa.Column("manual_override_status", sa.String(length=40)),
            sa.Column("max_concurrent_conversations", sa.Integer()),
            sa.Column("last_seen_at", sa.DateTime(timezone=True)),
            sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text())),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
            sa.UniqueConstraint("person_id", name="uq_inbox_agent_presence_person"),
        )
        op.create_index(
            "ix_inbox_agent_presence_status",
            "inbox_agent_presence",
            ["status"],
        )
        op.create_index(
            "ix_inbox_agent_presence_last_seen_at",
            "inbox_agent_presence",
            ["last_seen_at"],
        )

    if not _has_table("inbox_conversation_assignments"):
        op.create_table(
            "inbox_conversation_assignments",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("service_team_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("person_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("assigned_by_person_id", postgresql.UUID(as_uuid=True)),
            sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text())),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
            sa.ForeignKeyConstraint(["conversation_id"], ["inbox_conversations.id"]),
            sa.ForeignKeyConstraint(["service_team_id"], ["service_teams.id"]),
        )
        op.create_index(
            "uq_inbox_conversation_one_active_assignment",
            "inbox_conversation_assignments",
            ["conversation_id"],
            unique=True,
            postgresql_where=sa.text("is_active IS TRUE"),
        )
        op.create_index(
            "ix_inbox_conversation_assignments_person",
            "inbox_conversation_assignments",
            ["person_id", "is_active"],
        )
        op.create_index(
            "ix_inbox_conversation_assignments_team",
            "inbox_conversation_assignments",
            ["service_team_id", "is_active"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    for table_name in ("inbox_conversation_assignments", "inbox_agent_presence"):
        if _has_table(table_name):
            op.drop_table(table_name)
