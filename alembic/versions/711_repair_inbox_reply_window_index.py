"""Repair an interrupted concurrent reply-window index build.

Revision 710 can finish after a lock-timeout retry while PostgreSQL retains an
invalid same-name index from the interrupted build. This forward-only repair
preserves a usable index and rebuilds only the exact index owned by 710.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "711_repair_inbox_reply_window_index"
down_revision: str | None = "710_inbox_reply_window_projection"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_inbox_conversations_reply_window"
_CREATE = (
    f"CREATE INDEX CONCURRENTLY {_INDEX} ON public.inbox_conversations "
    "(channel_type, last_qualifying_inbound_at, status, id) "
    "WHERE last_qualifying_inbound_at IS NOT NULL"
)


def _index_state(bind: sa.engine.Connection) -> tuple[bool, bool, bool, bool] | None:
    row = bind.execute(
        sa.text(
            """
            SELECT
                COALESCE(
                    index_class.relkind = 'i'
                    AND table_namespace.nspname = 'public'
                    AND table_class.relname = 'inbox_conversations'
                    AND access_method.amname = 'btree'
                    AND NOT index_data.indisunique
                    AND index_data.indislive
                    AND index_data.indnkeyatts = 4
                    AND index_data.indnatts = 4
                    AND index_data.indexprs IS NULL
                    AND NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conindid = index_class.oid
                    )
                    AND ARRAY(
                        SELECT pg_get_indexdef(index_class.oid, position, true)
                        FROM generate_series(1, 4) AS position
                    ) = ARRAY[
                        'channel_type', 'last_qualifying_inbound_at',
                        'status', 'id'
                    ]
                    AND pg_get_expr(index_data.indpred, index_data.indrelid)
                        IN (
                            'last_qualifying_inbound_at IS NOT NULL',
                            '(last_qualifying_inbound_at IS NOT NULL)'
                        ),
                    false
                ) AS owned_shape,
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
            LEFT JOIN pg_class AS table_class ON table_class.oid = index_data.indrelid
            LEFT JOIN pg_namespace AS table_namespace
              ON table_namespace.oid = table_class.relnamespace
            LEFT JOIN pg_am AS access_method
              ON access_method.oid = index_class.relam
            WHERE namespace.nspname = 'public'
              AND index_class.relname = :name
            """
        ),
        {"name": _INDEX},
    ).one_or_none()
    if row is None:
        return None
    return bool(row.owned_shape), bool(row.valid), bool(row.ready), bool(row.building)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '60s'")
        op.execute("SET statement_timeout = '15min'")
        try:
            state = _index_state(bind)
            if state is not None:
                owned_shape, valid, ready, building = state
                if not owned_shape or building:
                    raise RuntimeError(
                        f"public.{_INDEX} is not an idle index owned by revision 710"
                    )
                if valid and ready:
                    return
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS public.{_INDEX}")
            op.execute(_CREATE)
            if _index_state(bind) != (True, True, True, False):
                raise RuntimeError(f"public.{_INDEX} is not valid and ready")
        finally:
            op.execute("RESET statement_timeout")
            op.execute("RESET lock_timeout")


def downgrade() -> None:
    # Revision 710 owns the index; reversing this repair retains it.
    pass
