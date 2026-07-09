"""add_subscriber_nin

Revision ID: 224_add_subscriber_nin
Revises: 223_work_order_dispatch_foundation
Create Date: 2026-07-09
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "224_add_subscriber_nin"
down_revision = "223_work_order_dispatch_foundation"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_column(table_name: str, column_name: str) -> bool:
    return any(
        column["name"] == column_name
        for column in _inspector().get_columns(table_name)
    )


def upgrade() -> None:
    if not _has_column("subscribers", "nin"):
        op.add_column(
            "subscribers",
            sa.Column("nin", sa.String(length=11), nullable=True),
        )


def downgrade() -> None:
    if _has_column("subscribers", "nin"):
        op.drop_column("subscribers", "nin")
