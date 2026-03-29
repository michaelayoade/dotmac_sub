"""add system-level table column default config

Revision ID: f1c2d3e4a5b6
Revises: e7f9a2c4d1b0
Create Date: 2026-02-24 14:15:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f1c2d3e4a5b6"
down_revision: str | None = "e7f9a2c4d1b0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "table_column_default_config",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("table_key", sa.String(length=120), nullable=False),
        sa.Column("column_key", sa.String(length=120), nullable=False),
        sa.Column("display_order", sa.Integer(), nullable=False),
        sa.Column("is_visible", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "table_key",
            "column_key",
            name="uq_table_column_default_config_table_column",
        ),
    )
    op.create_index(
        "ix_table_column_default_config_table_order",
        "table_column_default_config",
        ["table_key", "display_order"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_table_column_default_config_table_order",
        table_name="table_column_default_config",
    )
    op.drop_table("table_column_default_config")
