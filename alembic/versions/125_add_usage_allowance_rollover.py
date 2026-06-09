"""Add rollover_enabled to usage_allowances.

Revision ID: 125_add_usage_allowance_rollover
Revises: 124_add_support_comment_metadata
Create Date: 2026-06-09

Additive flag so a capped plan's unused allowance can carry into the next
period's quota bucket (migrated from Splynx fup_limits.rollover_data).
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "125_add_usage_allowance_rollover"
down_revision = "124_add_support_comment_metadata"
branch_labels = None
depends_on = None

_TABLE = "usage_allowances"
_COL = "rollover_enabled"


def upgrade() -> None:
    bind = op.get_bind()
    cols = {c["name"] for c in inspect(bind).get_columns(_TABLE)}
    if _COL not in cols:
        op.add_column(
            _TABLE,
            sa.Column(_COL, sa.Boolean(), nullable=False, server_default=sa.false()),
        )


def downgrade() -> None:
    op.drop_column(_TABLE, _COL)
