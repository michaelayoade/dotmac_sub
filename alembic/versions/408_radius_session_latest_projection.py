"""Bound CRM latest-session reads to one row per subscription.

Revision ID: 408_radius_session_latest_projection
Revises: 407_retire_parallel_radius_refresh

The CRM subscriber projection ranks accounting history by the newest observed
timestamp. This expression index supports that exact bounded query without
locking the hot accounting table for a normal index build.
"""

from __future__ import annotations

from alembic import op
from scripts.migration.radius_session_latest_index import (
    DROP_POSTGRES_SQL,
    INDEX_EXPRESSION,
    INDEX_NAME,
    TABLE_NAME,
    ensure_postgres_index,
)

revision = "408_radius_session_latest_projection"
down_revision = "407_retire_parallel_radius_refresh"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            ensure_postgres_index(bind, op.execute)
    else:
        op.execute(
            f"CREATE INDEX IF NOT EXISTS {INDEX_NAME} "
            f"ON {TABLE_NAME} ({INDEX_EXPRESSION})"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(DROP_POSTGRES_SQL)
    else:
        op.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")
