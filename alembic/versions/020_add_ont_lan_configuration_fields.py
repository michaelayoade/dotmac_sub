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


def upgrade() -> None:
    # Add LAN configuration fields to ont_units table
    op.add_column(
        "ont_units",
        sa.Column("lan_gateway_ip", sa.String(64), nullable=True),
    )
    op.add_column(
        "ont_units",
        sa.Column("lan_subnet_mask", sa.String(64), nullable=True),
    )
    op.add_column(
        "ont_units",
        sa.Column("lan_dhcp_enabled", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "ont_units",
        sa.Column("lan_dhcp_start", sa.String(64), nullable=True),
    )
    op.add_column(
        "ont_units",
        sa.Column("lan_dhcp_end", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ont_units", "lan_dhcp_end")
    op.drop_column("ont_units", "lan_dhcp_start")
    op.drop_column("ont_units", "lan_dhcp_enabled")
    op.drop_column("ont_units", "lan_subnet_mask")
    op.drop_column("ont_units", "lan_gateway_ip")
