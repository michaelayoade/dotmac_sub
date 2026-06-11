"""Add wrong-code lockout columns to mfa_methods.

Revision ID: 138_add_mfa_attempt_lockout
Revises: 137_extend_user_invite_expiry
Create Date: 2026-06-11
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "138_add_mfa_attempt_lockout"
down_revision = "137_extend_user_invite_expiry"
branch_labels = None
depends_on = None

_TABLE = "mfa_methods"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns(_TABLE)}
    if "failed_attempts" not in columns:
        op.add_column(
            _TABLE,
            sa.Column(
                "failed_attempts",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        )
    if "locked_until" not in columns:
        op.add_column(
            _TABLE,
            sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns(_TABLE)}
    if "locked_until" in columns:
        op.drop_column(_TABLE, "locked_until")
    if "failed_attempts" in columns:
        op.drop_column(_TABLE, "failed_attempts")
