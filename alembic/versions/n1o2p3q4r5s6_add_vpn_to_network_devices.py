"""Add WireGuard VPN selection to network devices.

Revision ID: n1o2p3q4r5s6
Revises: m2n3o4p5q6r
Create Date: 2026-02-02
"""

from alembic import op
import sqlalchemy as sa


revision = "n1o2p3q4r5s6"
down_revision = "m2n3o4p5q6r"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "network_devices",
        sa.Column("wireguard_server_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_network_devices_wireguard_server_id",
        "network_devices",
        "wireguard_servers",
        ["wireguard_server_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_network_devices_wireguard_server_id",
        "network_devices",
        type_="foreignkey",
    )
    op.drop_column("network_devices", "wireguard_server_id")
