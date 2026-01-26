"""Add IPv6 fields to WireGuard servers and peers.

Revision ID: p3q4r5s6t7u8
Revises: o2p3q4r5s6t7
Create Date: 2026-02-03
"""

from alembic import op
import sqlalchemy as sa


revision = "p3q4r5s6t7u8"
down_revision = "o2p3q4r5s6t7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "wireguard_servers",
        sa.Column("vpn_address_v6", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "wireguard_peers",
        sa.Column("peer_address_v6", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("wireguard_peers", "peer_address_v6")
    op.drop_column("wireguard_servers", "vpn_address_v6")
