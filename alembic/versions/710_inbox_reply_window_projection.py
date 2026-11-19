"""Persist and index the Team Inbox Meta reply-window projection.

The expand step adds a nullable conversation projection, backfills Meta
conversations in committed batches, verifies exact parity with the authoritative
message chronology, and builds the covering index concurrently. The application
writer advances the projection transactionally with each qualifying inbound.

Revision ID: 710_inbox_reply_window_projection
Revises: 709_inbox_latest_visible_message_index
Create Date: 2026-11-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "710_inbox_reply_window_projection"
down_revision: str | None = "709_inbox_latest_visible_message_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_inbox_conversations_reply_window"
_BATCH_SIZE = 5000
_META_CHANNELS_SQL = "('whatsapp', 'facebook_messenger', 'instagram_dm')"


def _postgresql_backfill() -> None:
    bind = op.get_bind()
    statement = sa.text(
        f"""
        WITH batch AS (
            SELECT
                conversation.id,
                MAX(COALESCE(message.received_at, message.created_at)) AS projected_at
            FROM inbox_conversations AS conversation
            JOIN inbox_messages AS message
              ON message.conversation_id = conversation.id
            WHERE conversation.channel_type IN {_META_CHANNELS_SQL}
              AND conversation.last_qualifying_inbound_at IS NULL
              AND message.direction = 'inbound'
              AND COALESCE(
                    message.metadata ->> 'reply_window_qualifying',
                    'true'
                  ) <> 'false'
            GROUP BY conversation.id
            ORDER BY conversation.id
            LIMIT :batch_size
        )
        UPDATE inbox_conversations AS conversation
        SET last_qualifying_inbound_at = batch.projected_at
        FROM batch
        WHERE conversation.id = batch.id
        """
    )
    while True:
        result = bind.execute(statement, {"batch_size": _BATCH_SIZE})
        if int(result.rowcount or 0) < _BATCH_SIZE:
            break


def _verify_projection() -> None:
    bind = op.get_bind()
    metadata_predicate = (
        "COALESCE(message.metadata ->> 'reply_window_qualifying', 'true') <> 'false'"
        if bind.dialect.name == "postgresql"
        else "COALESCE(json_extract(message.metadata, '$.reply_window_qualifying'), 1) <> 0"
    )
    drift = bind.execute(
        sa.text(
            f"""
            WITH expected AS (
                SELECT
                    conversation.id,
                    MAX(COALESCE(message.received_at, message.created_at)) AS projected_at
                FROM inbox_conversations AS conversation
                LEFT JOIN inbox_messages AS message
                  ON message.conversation_id = conversation.id
                 AND message.direction = 'inbound'
                 AND {metadata_predicate}
                WHERE conversation.channel_type IN {_META_CHANNELS_SQL}
                GROUP BY conversation.id
            )
            SELECT COUNT(*)
            FROM inbox_conversations AS conversation
            JOIN expected ON expected.id = conversation.id
            WHERE conversation.channel_type IN {_META_CHANNELS_SQL}
              AND conversation.last_qualifying_inbound_at
                  IS DISTINCT FROM expected.projected_at
            """
        )
    ).scalar_one()
    if int(drift) != 0:
        raise RuntimeError(
            "Team Inbox reply-window projection verification found "
            f"{int(drift)} mismatched conversations"
        )


def upgrade() -> None:
    op.add_column(
        "inbox_conversations",
        sa.Column("last_qualifying_inbound_at", sa.DateTime(timezone=True)),
    )
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("SET lock_timeout = '5s'")
            op.execute("SET statement_timeout = '15min'")
            _postgresql_backfill()
            _verify_projection()
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX} "
                "ON inbox_conversations "
                "(channel_type, last_qualifying_inbound_at, status, id) "
                "WHERE last_qualifying_inbound_at IS NOT NULL"
            )
            op.execute("RESET statement_timeout")
            op.execute("RESET lock_timeout")
        return

    op.execute(
        f"""
        UPDATE inbox_conversations AS conversation
        SET last_qualifying_inbound_at = (
            SELECT MAX(COALESCE(message.received_at, message.created_at))
            FROM inbox_messages AS message
            WHERE message.conversation_id = conversation.id
              AND message.direction = 'inbound'
              AND COALESCE(
                    json_extract(message.metadata, '$.reply_window_qualifying'),
                    1
                  ) <> 0
        )
        WHERE conversation.channel_type IN {_META_CHANNELS_SQL}
        """
    )
    _verify_projection()
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {_INDEX} ON inbox_conversations "
        "(channel_type, last_qualifying_inbound_at, status, id) "
        "WHERE last_qualifying_inbound_at IS NOT NULL"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("SET lock_timeout = '5s'")
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
            op.execute("RESET lock_timeout")
    else:
        op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
    op.drop_column("inbox_conversations", "last_qualifying_inbound_at")
