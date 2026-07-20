"""Add webhook endpoint delivery controls.

Revision ID: 187_webhook_endpoint_delivery_controls
Revises: 186_integration_hook_timeout_seconds
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "187_webhook_endpoint_delivery_controls"
down_revision = "186_integration_hook_timeout_seconds"
branch_labels = None
depends_on = None

_TABLE = "webhook_endpoints"
_COLUMNS = (
    "delivery_timeout_seconds",
    "max_retries",
    "retry_backoff_seconds",
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns(_TABLE)}
    for column_name in _COLUMNS:
        if column_name not in existing:
            op.add_column(_TABLE, sa.Column(column_name, sa.Integer(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns(_TABLE)}
    for column_name in reversed(_COLUMNS):
        if column_name in existing:
            op.drop_column(_TABLE, column_name)
