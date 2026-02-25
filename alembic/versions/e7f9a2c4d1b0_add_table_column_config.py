"""add table column configuration table

Revision ID: e7f9a2c4d1b0
Revises: 9f3e2a1b4c7d
Create Date: 2026-02-24 12:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7f9a2c4d1b0"
down_revision: str | None = "9f3e2a1b4c7d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "table_column_config",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("table_key", sa.String(length=120), nullable=False),
        sa.Column("column_key", sa.String(length=120), nullable=False),
        sa.Column("display_order", sa.Integer(), nullable=False),
        sa.Column("is_visible", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["subscribers.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "table_key",
            "column_key",
            name="uq_table_column_config_user_table_column",
        ),
    )
    op.create_index(
        "ix_table_column_config_user_table_order",
        "table_column_config",
        ["user_id", "table_key", "display_order"],
        unique=False,
    )
    op.create_index(
        "ix_subscribers_status_is_active_created_at",
        "subscribers",
        ["status", "is_active", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_subscribers_email_created_at",
        "subscribers",
        ["email", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_subscribers_email_created_at", table_name="subscribers"
    )
    op.drop_index("ix_subscribers_status_is_active_created_at", table_name="subscribers")
    op.drop_index(
        "ix_table_column_config_user_table_order", table_name="table_column_config"
    )
    op.drop_table("table_column_config")
