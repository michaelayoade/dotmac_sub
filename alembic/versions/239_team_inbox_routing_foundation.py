"""Add team inbox routing foundation.

Revision ID: 239_team_inbox_routing_foundation
Revises: 238_field_map_asset_location_provenance
Create Date: 2026-07-10
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "239_team_inbox_routing_foundation"
down_revision = "238_field_map_asset_location_provenance"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    if not _has_table("team_inbox_email_routes"):
        op.create_table(
            "team_inbox_email_routes",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("service_team_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("email_address", sa.String(length=255), nullable=False),
            sa.Column(
                "is_primary", sa.Boolean(), nullable=False, server_default=sa.false()
            ),
            sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
            sa.Column(
                "is_active", sa.Boolean(), nullable=False, server_default=sa.true()
            ),
            sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text())),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
            sa.ForeignKeyConstraint(["service_team_id"], ["service_teams.id"]),
            sa.UniqueConstraint(
                "service_team_id",
                "email_address",
                name="uq_team_inbox_email_routes_team_address",
            ),
        )
        op.create_index(
            "ix_team_inbox_email_routes_address_active",
            "team_inbox_email_routes",
            ["email_address", "is_active"],
        )
        op.create_index(
            "ix_team_inbox_email_routes_team",
            "team_inbox_email_routes",
            ["service_team_id"],
        )

    if not _has_table("inbox_conversations"):
        op.create_table(
            "inbox_conversations",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("subscriber_id", postgresql.UUID(as_uuid=True)),
            sa.Column("primary_service_team_id", postgresql.UUID(as_uuid=True)),
            sa.Column("channel_type", sa.String(length=40), nullable=False),
            sa.Column("status", sa.String(length=40), nullable=False),
            sa.Column("subject", sa.String(length=200)),
            sa.Column("contact_address", sa.String(length=255)),
            sa.Column("external_thread_id", sa.String(length=255)),
            sa.Column("first_message_at", sa.DateTime(timezone=True)),
            sa.Column("last_message_at", sa.DateTime(timezone=True)),
            sa.Column(
                "is_active", sa.Boolean(), nullable=False, server_default=sa.true()
            ),
            sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text())),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
            sa.ForeignKeyConstraint(["subscriber_id"], ["subscribers.id"]),
            sa.ForeignKeyConstraint(["primary_service_team_id"], ["service_teams.id"]),
        )
        op.create_index(
            "ix_inbox_conversations_subscriber",
            "inbox_conversations",
            ["subscriber_id"],
        )
        op.create_index(
            "ix_inbox_conversations_primary_team",
            "inbox_conversations",
            ["primary_service_team_id"],
        )
        op.create_index(
            "ix_inbox_conversations_status_last",
            "inbox_conversations",
            ["status", "last_message_at"],
        )
        op.create_index(
            "ix_inbox_conversations_external_thread",
            "inbox_conversations",
            ["channel_type", "external_thread_id"],
        )

    if not _has_table("inbox_conversation_teams"):
        op.create_table(
            "inbox_conversation_teams",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("service_team_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("role", sa.String(length=40), nullable=False),
            sa.Column("source", sa.String(length=40), nullable=False),
            sa.Column(
                "is_active", sa.Boolean(), nullable=False, server_default=sa.true()
            ),
            sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text())),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
            sa.ForeignKeyConstraint(["conversation_id"], ["inbox_conversations.id"]),
            sa.ForeignKeyConstraint(["service_team_id"], ["service_teams.id"]),
            sa.UniqueConstraint(
                "conversation_id",
                "service_team_id",
                name="uq_inbox_conversation_teams_conversation_team",
            ),
        )
        op.create_index(
            "ix_inbox_conversation_teams_team_role",
            "inbox_conversation_teams",
            ["service_team_id", "role"],
        )

    if not _has_table("inbox_messages"):
        op.create_table(
            "inbox_messages",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("channel_type", sa.String(length=40), nullable=False),
            sa.Column("direction", sa.String(length=40), nullable=False),
            sa.Column("subject", sa.String(length=200)),
            sa.Column("body", sa.Text()),
            sa.Column("external_message_id", sa.String(length=255)),
            sa.Column("external_thread_id", sa.String(length=255)),
            sa.Column("from_address", sa.String(length=255)),
            sa.Column("to_addresses", postgresql.JSONB(astext_type=sa.Text())),
            sa.Column("cc_addresses", postgresql.JSONB(astext_type=sa.Text())),
            sa.Column("sent_at", sa.DateTime(timezone=True)),
            sa.Column("received_at", sa.DateTime(timezone=True)),
            sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text())),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
            sa.ForeignKeyConstraint(["conversation_id"], ["inbox_conversations.id"]),
        )
        op.create_index(
            "ix_inbox_messages_conversation",
            "inbox_messages",
            ["conversation_id", "created_at"],
        )
        op.create_index(
            "uq_inbox_messages_inbound_external",
            "inbox_messages",
            ["channel_type", "external_message_id"],
            unique=True,
            postgresql_where=sa.text(
                "external_message_id IS NOT NULL AND direction = 'inbound'"
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    for table_name in (
        "inbox_messages",
        "inbox_conversation_teams",
        "inbox_conversations",
        "team_inbox_email_routes",
    ):
        if _has_table(table_name):
            op.drop_table(table_name)
