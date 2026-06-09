"""Add metadata to support ticket comments.

Revision ID: 124_add_support_comment_metadata
Revises: 123_add_reseller_users_fks
Create Date: 2026-06-09
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "124_add_support_comment_metadata"
down_revision = "123_add_reseller_users_fks"
branch_labels = None
depends_on = None

_TABLE = "support_ticket_comments"
_COLUMN = "metadata"


def upgrade() -> None:
    bind = op.get_bind()
    # SQLite (tests) builds the schema from model metadata via create_all
    # rather than running migrations; the model carries this column there.
    if bind.dialect.name == "sqlite":
        return
    inspector = inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns(_TABLE)}
    if _COLUMN not in columns:
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.JSON(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    inspector = inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns(_TABLE)}
    if _COLUMN in columns:
        op.drop_column(_TABLE, _COLUMN)
