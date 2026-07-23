"""Bound CRM latest-session reads to one row per subscription.

Revision ID: 408_radius_session_latest_projection
Revises: 407_retire_parallel_radius_refresh

The CRM subscriber projection ranks accounting history by the newest observed
timestamp. This expression index supports that exact bounded query without
locking the hot accounting table for a normal index build.
"""

from __future__ import annotations

from alembic import op

revision = "408_radius_session_latest_projection"
down_revision = "407_retire_parallel_radius_refresh"
branch_labels = None
depends_on = None

_INDEX = "ix_radius_accounting_sessions_subscription_latest"
_TABLE = "radius_accounting_sessions"
_EXPRESSION = (
    "subscription_id, "
    "(COALESCE(last_update_at, session_start, created_at)) DESC, "
    "id DESC"
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX} "
                f"ON {_TABLE} ({_EXPRESSION})"
            )
    else:
        op.execute(f"CREATE INDEX IF NOT EXISTS {_INDEX} ON {_TABLE} ({_EXPRESSION})")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
    else:
        op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
