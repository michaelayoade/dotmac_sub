"""Add LAN configuration fields to ONT units.

LAN configuration (gateway IP, subnet, DHCP settings) was previously stored
only in ServiceOrder.execution_context, which required a subscriber assignment.
This migration adds these fields directly to the ONT model so OLT/ONT
provisioning actions can be independent of internet service orders.

Revision ID: 020_add_ont_lan_configuration_fields
Revises: 48d94c532a05
Create Date: 2026-04-14
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "020_add_ont_lan_configuration_fields"
down_revision = "48d94c532a05"
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
    # Add LAN configuration fields to ont_units table
    _add_column_if_missing(
        "ont_units",
        sa.Column("lan_gateway_ip", sa.String(64), nullable=True),
    )
    _add_column_if_missing(
        "ont_units",
        sa.Column("lan_subnet_mask", sa.String(64), nullable=True),
    )
    _add_column_if_missing(
        "ont_units",
        sa.Column("lan_dhcp_enabled", sa.Boolean(), nullable=True),
    )
    _add_column_if_missing(
        "ont_units",
        sa.Column("lan_dhcp_start", sa.String(64), nullable=True),
    )
    _add_column_if_missing(
        "ont_units",
        sa.Column("lan_dhcp_end", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    _drop_column_if_exists("ont_units", "lan_dhcp_end")
    _drop_column_if_exists("ont_units", "lan_dhcp_start")
    _drop_column_if_exists("ont_units", "lan_dhcp_enabled")
    _drop_column_if_exists("ont_units", "lan_subnet_mask")
    _drop_column_if_exists("ont_units", "lan_gateway_ip")
