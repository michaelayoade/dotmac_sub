"""Add covering indexes for bounded Inbox agent analytics.

The report reads assignment, resolution-event, and message chronology facts
without backfill or row mutation. PostgreSQL builds each additive index
concurrently with a five-second lock budget and a fifteen-minute statement
budget. A failed build is safe to retry; the migration drops only its own new
index names before rebuilding. The application query remains compatible before,
during, and after each build. Downgrade removes only these derived read indexes.

Revision ID: 564_inbox_agent_analytics_indexes
Revises: 563_topup_reconcile_leases
Create Date: 2026-08-28
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "564_inbox_agent_analytics_indexes"
down_revision: str | None = "563_topup_reconcile_leases"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEXES: tuple[tuple[str, str, str], ...] = (
    (
        "ix_inbox_conversations_agent_analytics",
        "inbox_conversations",
        "first_message_at, id",
    ),
    (
        "ix_inbox_assignments_agent_analytics",
        "inbox_conversation_assignments",
        "assigned_at, person_id, service_team_id, conversation_id",
    ),
    (
        "ix_inbox_status_event_agent_analytics",
        "inbox_status_transition_events",
        "status, occurred_at, actor_person_id, conversation_id",
    ),
    (
        "ix_inbox_messages_agent_analytics",
        "inbox_messages",
        "direction, created_at, conversation_id",
    ),
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("SET lock_timeout = '5s'")
            op.execute("SET statement_timeout = '15min'")
            for name, table, columns in _INDEXES:
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
                op.execute(f"CREATE INDEX CONCURRENTLY {name} ON {table} ({columns})")
            op.execute("RESET statement_timeout")
            op.execute("RESET lock_timeout")
        return

    for name, table, columns in _INDEXES:
        op.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({columns})")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("SET lock_timeout = '5s'")
            for name, _table, _columns in reversed(_INDEXES):
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
            op.execute("RESET lock_timeout")
        return

    for name, _table, _columns in reversed(_INDEXES):
        op.execute(f"DROP INDEX IF EXISTS {name}")
