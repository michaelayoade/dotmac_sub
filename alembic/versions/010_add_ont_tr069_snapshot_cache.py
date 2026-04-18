"""add ont tr069 snapshot cache

Revision ID: 010_add_ont_tr069_snapshot_cache
Revises: 009_enforce_single_active_tr069_link_per_ont
Create Date: 2026-04-02
"""

import sqlalchemy as sa

from alembic import op

revision = "010_add_ont_tr069_snapshot_cache"
down_revision = "009_enforce_single_active_tr069_link_per_ont"
branch_labels = None
depends_on = None


def _column_exists(table_name: str, column_name: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    return column_name in {
        column["name"] for column in inspector.get_columns(table_name)
    }


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if not _column_exists(table_name, column.name):
        op.add_column(table_name, column)


def _drop_column_if_exists(table_name: str, column_name: str) -> None:
    if _column_exists(table_name, column_name):
        op.drop_column(table_name, column_name)


def upgrade() -> None:
    _add_column_if_missing(
        "ont_units",
        sa.Column("tr069_last_snapshot", sa.JSON(), nullable=True),
    )
    _add_column_if_missing(
        "ont_units",
        sa.Column("tr069_last_snapshot_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    _drop_column_if_exists("ont_units", "tr069_last_snapshot_at")
    _drop_column_if_exists("ont_units", "tr069_last_snapshot")
