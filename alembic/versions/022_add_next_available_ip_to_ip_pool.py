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


def upgrade() -> None:
    # Add next_available_ip column - stores the next IP that can be allocated
    op.add_column(
        "ip_pools",
        sa.Column("next_available_ip", sa.String(64), nullable=True),
    )
    # Add available_count column - stores how many IPs are still available
    op.add_column(
        "ip_pools",
        sa.Column("available_count", sa.Integer, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ip_pools", "available_count")
    op.drop_column("ip_pools", "next_available_ip")
