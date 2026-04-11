"""optimize tr069 inform storage

Revision ID: 014_optimize_tr069_inform_storage
Revises: ed9e5aad1101
Create Date: 2026-04-11
"""

from __future__ import annotations

from sqlalchemy import text

from alembic import op

revision = "014_optimize_tr069_inform_storage"
down_revision = "ed9e5aad1101"
branch_labels = None
depends_on = None


def _index_exists(index_name: str) -> bool:
    conn = op.get_bind()
    row = conn.execute(
        text(
            """
            SELECT 1
            FROM pg_indexes
            WHERE schemaname = current_schema()
              AND indexname = :index_name
            """
        ),
        {"index_name": index_name},
    ).fetchone()
    return row is not None


def _create_index(name: str, ddl: str) -> None:
    if not _index_exists(name):
        op.execute(text(ddl))


def upgrade() -> None:
    op.execute(
        text(
            """
            DELETE FROM tr069_parameters keep
            USING tr069_parameters drop_row
            WHERE keep.device_id = drop_row.device_id
              AND keep.name = drop_row.name
              AND (
                    keep.updated_at < drop_row.updated_at
                    OR (
                        keep.updated_at = drop_row.updated_at
                        AND keep.id::text < drop_row.id::text
                    )
              )
            """
        )
    )
    _create_index(
        "uq_tr069_parameters_device_name",
        """
        CREATE UNIQUE INDEX uq_tr069_parameters_device_name
        ON tr069_parameters (device_id, name)
        """,
    )
    _create_index(
        "ix_tr069_parameters_device_updated_at",
        """
        CREATE INDEX ix_tr069_parameters_device_updated_at
        ON tr069_parameters (device_id, updated_at DESC)
        """,
    )
    _create_index(
        "ix_tr069_sessions_device_started_at",
        """
        CREATE INDEX ix_tr069_sessions_device_started_at
        ON tr069_sessions (device_id, started_at DESC)
        """,
    )
    _create_index(
        "ix_tr069_sessions_created_at",
        """
        CREATE INDEX ix_tr069_sessions_created_at
        ON tr069_sessions (created_at)
        """,
    )


def downgrade() -> None:
    op.execute(text("DROP INDEX IF EXISTS ix_tr069_sessions_created_at"))
    op.execute(text("DROP INDEX IF EXISTS ix_tr069_sessions_device_started_at"))
    op.execute(text("DROP INDEX IF EXISTS ix_tr069_parameters_device_updated_at"))
    op.execute(text("DROP INDEX IF EXISTS uq_tr069_parameters_device_name"))
