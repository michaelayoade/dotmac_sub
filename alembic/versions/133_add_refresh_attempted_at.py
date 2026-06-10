"""Add refresh_attempted_at to radius_accounting_sessions.

Round-robin key for the importer's open-session refresh pass: ordering by
least-recently-attempted prevents unchanging ghost rows from pinning the
refresh window and starving live sessions of last_update_at refreshes.

Revision ID: 133_add_refresh_attempted_at
Revises: 132_radius_session_observability
Create Date: 2026-06-10
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "133_add_refresh_attempted_at"
down_revision = "132_radius_session_observability"
branch_labels = None
depends_on = None

_TABLE = "radius_accounting_sessions"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    inspector = inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return
    columns = {item["name"] for item in inspector.get_columns(_TABLE)}
    if "refresh_attempted_at" not in columns:
        op.add_column(
            _TABLE, sa.Column("refresh_attempted_at", sa.DateTime(timezone=True))
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    op.drop_column(_TABLE, "refresh_attempted_at")
