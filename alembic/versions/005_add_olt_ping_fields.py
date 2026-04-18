"""Add OLT ping reachability fields.

Adds fields to track network-level reachability via ping:
- last_ping_at: When the OLT was last pinged
- last_ping_ok: Whether the ping succeeded

Revision ID: 005_add_olt_ping_fields
Revises: 004_add_olt_polling_health_fields
Create Date: 2026-04-01

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "005_add_olt_ping_fields"
down_revision = "003_add_olt_polling_health_fields"
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
        "olt_devices",
        sa.Column("last_ping_at", sa.DateTime(timezone=True), nullable=True),
    )
    _add_column_if_missing(
        "olt_devices",
        sa.Column("last_ping_ok", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    _drop_column_if_exists("olt_devices", "last_ping_ok")
    _drop_column_if_exists("olt_devices", "last_ping_at")
