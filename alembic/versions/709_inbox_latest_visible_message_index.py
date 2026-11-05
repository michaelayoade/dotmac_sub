"""Index the latest visible Inbox message lookup.

The sidebar's needs-response projection asks for the newest non-internal
message in each actionable conversation ordered by effective message time,
created time, and identifier. The existing conversation/created index cannot
satisfy that expression order. This additive covering index is built
concurrently with explicit lock and statement budgets.

Revision ID: 709_inbox_latest_visible_message_index
Revises: 708_vendor_supplier_flow
Create Date: 2026-11-05
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "709_inbox_latest_visible_message_index"
down_revision: str | None = "708_vendor_supplier_flow"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_inbox_messages_latest_visible"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("SET lock_timeout = '5s'")
            op.execute("SET statement_timeout = '15min'")
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX} "
                "ON inbox_messages ("
                "conversation_id, "
                "COALESCE(received_at, sent_at, created_at) DESC, "
                "created_at DESC, id DESC"
                ") INCLUDE (direction) WHERE direction <> 'internal'"
            )
            op.execute("RESET statement_timeout")
            op.execute("RESET lock_timeout")
        return

    op.execute(
        f"CREATE INDEX IF NOT EXISTS {_INDEX} ON inbox_messages ("
        "conversation_id, "
        "COALESCE(received_at, sent_at, created_at) DESC, "
        "created_at DESC, id DESC"
        ") WHERE direction <> 'internal'"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("SET lock_timeout = '5s'")
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
            op.execute("RESET lock_timeout")
        return

    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
