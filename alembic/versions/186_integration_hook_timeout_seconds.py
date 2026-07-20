"""Add per-hook execution timeout.

Revision ID: 186_integration_hook_timeout_seconds
Revises: 185_router_rest_api_username_width
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "186_integration_hook_timeout_seconds"
down_revision = "185_router_rest_api_username_width"
branch_labels = None
depends_on = None

_TABLE = "integration_hooks"
_COLUMN = "timeout_seconds"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns(_TABLE)}
    if _COLUMN in columns:
        return
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.Integer(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns(_TABLE)}
    if _COLUMN not in columns:
        return
    op.drop_column(_TABLE, _COLUMN)
