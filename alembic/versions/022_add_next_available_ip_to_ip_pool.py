"""Add next_available_ip and available_count to ip_pools.

Revision ID: 022_next_available_ip
Revises: 021_add_mgmt_ip_pool_to_provisioning_profile
Create Date: 2026-04-15
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers
revision = "022_next_available_ip"
down_revision = "021_add_mgmt_ip_pool_to_provisioning_profile"
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
    # Add next_available_ip column - stores the next IP that can be allocated
    _add_column_if_missing(
        "ip_pools",
        sa.Column("next_available_ip", sa.String(64), nullable=True),
    )
    # Add available_count column - stores how many IPs are still available
    _add_column_if_missing(
        "ip_pools",
        sa.Column("available_count", sa.Integer, nullable=True),
    )


def downgrade() -> None:
    _drop_column_if_exists("ip_pools", "available_count")
    _drop_column_if_exists("ip_pools", "next_available_ip")
