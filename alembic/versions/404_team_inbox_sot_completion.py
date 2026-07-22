"""Add Team Inbox observation ledger and operator read cursors.

Revision ID: 404_team_inbox_sot_completion
Revises: 403_service_change_reconciliation_evidence
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "404_team_inbox_sot_completion"
down_revision = "403_service_change_reconciliation_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "inbox_provider_observations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(length=80), nullable=False),
        sa.Column("provider_account_scope", sa.String(length=160), nullable=False),
        sa.Column("provider_event_id", sa.String(length=255), nullable=False),
        sa.Column("observation_kind", sa.String(length=40), nullable=False),
        sa.Column("channel_type", sa.String(length=40), nullable=False),
        sa.Column("external_message_id", sa.String(length=255)),
        sa.Column("external_thread_id", sa.String(length=255)),
        sa.Column("payload_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("normalized_payload", sa.JSON(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.Column("processing_status", sa.String(length=40), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True)),
        sa.Column("message_id", postgresql.UUID(as_uuid=True)),
        sa.Column("error_code", sa.String(length=120)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["inbox_conversations.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["message_id"], ["inbox_messages.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider",
            "provider_account_scope",
            "provider_event_id",
            name="uq_inbox_provider_observations_identity",
        ),
    )
    op.create_index(
        "ix_inbox_provider_observations_status",
        "inbox_provider_observations",
        ["processing_status", "recorded_at"],
    )
    op.create_index(
        "ix_inbox_provider_observations_message",
        "inbox_provider_observations",
        ["external_message_id", "observation_kind"],
    )

    op.create_table(
        "inbox_conversation_read_states",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("person_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("last_read_message_id", postgresql.UUID(as_uuid=True)),
        sa.Column("last_read_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["inbox_conversations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["last_read_message_id"], ["inbox_messages.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "conversation_id",
            "person_id",
            name="uq_inbox_conversation_read_states_person",
        ),
    )
    op.create_index(
        "ix_inbox_conversation_read_states_person",
        "inbox_conversation_read_states",
        ["person_id", "last_read_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_inbox_conversation_read_states_person",
        table_name="inbox_conversation_read_states",
    )
    op.drop_table("inbox_conversation_read_states")
    op.drop_index(
        "ix_inbox_provider_observations_message",
        table_name="inbox_provider_observations",
    )
    op.drop_index(
        "ix_inbox_provider_observations_status",
        table_name="inbox_provider_observations",
    )
    op.drop_table("inbox_provider_observations")
