"""Align radius active session indexes with query patterns.

Revision ID: k3l4m5n6o7p8
Revises: k1l2m3n4o5p7
Create Date: 2026-03-22
"""

from sqlalchemy import inspect, text

from alembic import op


revision = "k3l4m5n6o7p8"
down_revision = "k1l2m3n4o5p7"
branch_labels = None
depends_on = None


def _drop_if_exists(conn, table_name: str, name: str) -> None:
    """Drop an index only when it exists on the target table."""
    inspector = inspect(conn)
    existing_indexes = {index["name"] for index in inspector.get_indexes(table_name)}
    if name in existing_indexes:
        conn.execute(text(f"DROP INDEX IF EXISTS {name}"))


def _create_if_missing(conn, table_name: str, name: str, ddl: str) -> None:
    """Create an index only when it does not already exist on the target table."""
    inspector = inspect(conn)
    existing_indexes = {index["name"] for index in inspector.get_indexes(table_name)}
    if name not in existing_indexes:
        conn.execute(text(ddl))


def upgrade() -> None:
    conn = op.get_bind()

    _drop_if_exists(conn, "radius_active_sessions", "ix_radius_sessions_subscriber")
    _drop_if_exists(conn, "radius_active_sessions", "ix_radius_sessions_nas")
    _drop_if_exists(conn, "radius_active_sessions", "ix_radius_sessions_start")

    _create_if_missing(
        conn,
        "radius_active_sessions",
        "ix_radius_sessions_subscriber_start",
        "CREATE INDEX ix_radius_sessions_subscriber_start "
        "ON radius_active_sessions (subscriber_id, session_start DESC)",
    )
    _create_if_missing(
        conn,
        "radius_active_sessions",
        "ix_radius_sessions_nas_start",
        "CREATE INDEX ix_radius_sessions_nas_start "
        "ON radius_active_sessions (nas_device_id, session_start DESC)",
    )


def downgrade() -> None:
    conn = op.get_bind()

    _drop_if_exists(conn, "radius_active_sessions", "ix_radius_sessions_nas_start")
    _drop_if_exists(conn, "radius_active_sessions", "ix_radius_sessions_subscriber_start")

    _create_if_missing(
        conn,
        "radius_active_sessions",
        "ix_radius_sessions_subscriber",
        "CREATE INDEX ix_radius_sessions_subscriber "
        "ON radius_active_sessions (subscriber_id)",
    )
    _create_if_missing(
        conn,
        "radius_active_sessions",
        "ix_radius_sessions_nas",
        "CREATE INDEX ix_radius_sessions_nas "
        "ON radius_active_sessions (nas_device_id)",
    )
    _create_if_missing(
        conn,
        "radius_active_sessions",
        "ix_radius_sessions_start",
        "CREATE INDEX ix_radius_sessions_start "
        "ON radius_active_sessions (session_start DESC)",
    )
