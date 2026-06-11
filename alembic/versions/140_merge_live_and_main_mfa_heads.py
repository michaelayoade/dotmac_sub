"""Merge live and main MFA migration heads.

Revision ID: 140_merge_live_and_main_mfa_heads
Revises: 139_merge_local_mfa_and_service_extension_heads, 139_merge_mfa_and_service_extension_heads
Create Date: 2026-06-11
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "140_merge_live_and_main_mfa_heads"
down_revision = (
    "139_merge_local_mfa_and_service_extension_heads",
    "139_merge_mfa_and_service_extension_heads",
)
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
    else:
        op.alter_column(
            _TABLE,
            "failed_attempts",
            existing_type=sa.Integer(),
            nullable=False,
            server_default="0",
        )
    if "locked_until" not in columns:
        op.add_column(_TABLE, sa.Column("locked_until", sa.DateTime(timezone=True)))


def downgrade() -> None:
    pass
