"""add session-scoped Team Inbox presence evidence

Revision ID: 704_inbox_agent_presence_session_leases
Revises: 703_inbox_assignment_notification_preferences
Create Date: 2026-11-03
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "704_inbox_agent_presence_session_leases"
down_revision: str | None = "703_inbox_assignment_notification_preferences"
branch_labels = None
depends_on = None

_PRESENCE_TABLE = "inbox_agent_presence"
_LEASE_TABLE = "inbox_agent_presence_session_leases"


def upgrade() -> None:
    op.add_column(
        _PRESENCE_TABLE,
        sa.Column(
            "auto_offline",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.create_table(
        _LEASE_TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "person_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("system_users.id"),
            nullable=False,
        ),
        sa.Column(
            "auth_session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sessions.id"),
            nullable=False,
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "auth_session_id",
            name="uq_inbox_agent_presence_lease_auth_session",
        ),
    )
    op.create_index(
        "ix_inbox_agent_presence_lease_person_seen",
        _LEASE_TABLE,
        ["person_id", "last_seen_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_inbox_agent_presence_lease_person_seen", table_name=_LEASE_TABLE)
    op.drop_table(_LEASE_TABLE)
    op.drop_column(_PRESENCE_TABLE, "auto_offline")
