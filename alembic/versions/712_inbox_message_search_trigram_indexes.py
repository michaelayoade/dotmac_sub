"""Index every message-text branch of the Team Inbox substring search.

The bounded Inbox search uses OR across three message columns and four sender
names in message metadata. An unindexed branch can force a scan of the whole
message table even when the query asks for only 500 conversation IDs. Build
each index concurrently so message intake remains available. A retry repairs
only an interrupted index with the exact name and shape owned here.

Revision ID: 712_inbox_message_search_trigram_indexes
Revises: 711_repair_inbox_reply_window_index
"""

from collections.abc import Sequence
from dataclasses import dataclass

import sqlalchemy as sa

from alembic import op

revision: str = "712_inbox_message_search_trigram_indexes"
down_revision: str | None = "711_repair_inbox_reply_window_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


@dataclass(frozen=True, slots=True)
class SearchIndex:
    name: str
    expression: str
    column: str | None = None
    metadata_key: str | None = None


_INDEXES: tuple[SearchIndex, ...] = (
    SearchIndex("ix_inbox_msg_search_subject_trgm", "subject", column="subject"),
    SearchIndex("ix_inbox_msg_search_body_trgm", "body", column="body"),
    SearchIndex(
        "ix_inbox_msg_search_from_address_trgm",
        "from_address",
        column="from_address",
    ),
    SearchIndex(
        "ix_inbox_msg_search_from_name_trgm",
        "(CAST((metadata ->> 'from_name') AS VARCHAR))",
        metadata_key="from_name",
    ),
    SearchIndex(
        "ix_inbox_msg_search_contact_name_trgm",
        "(CAST((metadata ->> 'contact_name') AS VARCHAR))",
        metadata_key="contact_name",
    ),
    SearchIndex(
        "ix_inbox_msg_search_sender_name_trgm",
        "(CAST((metadata ->> 'sender_name') AS VARCHAR))",
        metadata_key="sender_name",
    ),
    SearchIndex(
        "ix_inbox_msg_search_profile_name_trgm",
        "(CAST((metadata ->> 'profile_name') AS VARCHAR))",
        metadata_key="profile_name",
    ),
)


def _index_state(
    bind: sa.engine.Connection, index: SearchIndex
) -> tuple[bool, bool, bool, bool] | None:
    row = bind.execute(
        sa.text(
            """
            SELECT
                table_namespace.nspname = 'public'
                    AND table_class.relname = 'inbox_messages'
                    AND access_method.amname = 'gin'
                    AND index_class.relkind = 'i'
                    AND NOT index_data.indisunique
                    AND index_data.indislive
                    AND index_data.indnkeyatts = 1
                    AND index_data.indnatts = 1
                    AND operator_class.opcname = 'gin_trgm_ops'
                    AND NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conindid = index_class.oid
                    ) AS owned_base,
                key_attribute.attname AS column_name,
                pg_get_expr(index_data.indexprs, index_data.indrelid)
                    AS expression,
                COALESCE(index_data.indisvalid, false) AS valid,
                COALESCE(index_data.indisready, false) AS ready,
                EXISTS (
                    SELECT 1 FROM pg_stat_progress_create_index
                    WHERE index_relid = index_class.oid
                ) AS building
            FROM pg_class AS index_class
            JOIN pg_namespace AS namespace
              ON namespace.oid = index_class.relnamespace
            LEFT JOIN pg_index AS index_data
              ON index_data.indexrelid = index_class.oid
            LEFT JOIN pg_class AS table_class
              ON table_class.oid = index_data.indrelid
            LEFT JOIN pg_namespace AS table_namespace
              ON table_namespace.oid = table_class.relnamespace
            LEFT JOIN pg_am AS access_method
              ON access_method.oid = index_class.relam
            LEFT JOIN pg_opclass AS operator_class
              ON operator_class.oid = index_data.indclass[0]
            LEFT JOIN pg_attribute AS key_attribute
              ON key_attribute.attrelid = table_class.oid
             AND key_attribute.attnum = index_data.indkey[0]
            WHERE namespace.nspname = 'public'
              AND index_class.relname = :name
            """
        ),
        {"name": index.name},
    ).one_or_none()
    if row is None:
        return None
    owned_key = (
        row.column_name == index.column and row.expression is None
        if index.column is not None
        else row.column_name is None
        and row.expression is not None
        and f"->> '{index.metadata_key}'" in row.expression
    )
    return (
        bool(row.owned_base and owned_key),
        bool(row.valid),
        bool(row.ready),
        bool(row.building),
    )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '5s'")
        op.execute("SET statement_timeout = '15min'")
        try:
            for index in _INDEXES:
                state = _index_state(bind, index)
                if state is not None:
                    owned, valid, ready, building = state
                    if not owned or building:
                        raise RuntimeError(
                            f"public.{index.name} is not an idle index owned "
                            "by revision 712"
                        )
                    if valid and ready:
                        continue
                    op.execute(f"DROP INDEX CONCURRENTLY public.{index.name}")
                op.execute(
                    f"CREATE INDEX CONCURRENTLY {index.name} "
                    "ON public.inbox_messages USING gin "
                    f"({index.expression} gin_trgm_ops)"
                )
                if _index_state(bind, index) != (True, True, True, False):
                    raise RuntimeError(f"public.{index.name} is not valid and ready")
        finally:
            op.execute("RESET statement_timeout")
            op.execute("RESET lock_timeout")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '5s'")
        try:
            for index in reversed(_INDEXES):
                state = _index_state(bind, index)
                if state is None:
                    continue
                owned, _valid, _ready, building = state
                if not owned or building:
                    raise RuntimeError(
                        f"public.{index.name} is not an idle index owned "
                        "by revision 712"
                    )
                op.execute(f"DROP INDEX CONCURRENTLY public.{index.name}")
        finally:
            op.execute("RESET lock_timeout")
