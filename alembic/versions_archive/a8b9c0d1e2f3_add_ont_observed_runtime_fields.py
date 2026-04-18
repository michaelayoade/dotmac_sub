"""Add observed runtime fields to ont_units.

Revision ID: a8b9c0d1e2f3
Revises: z7b8c9d0e1f2
Create Date: 2026-03-12
"""

import sqlalchemy as sa

from alembic import op

revision = "a8b9c0d1e2f3"
down_revision = "z7b8c9d0e1f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = {c["name"] for c in inspector.get_columns("ont_units")}

    if "mac_address" not in columns:
        op.add_column(
            "ont_units", sa.Column("mac_address", sa.String(length=64), nullable=True)
        )
    if "observed_wan_ip" not in columns:
        op.add_column(
            "ont_units",
            sa.Column("observed_wan_ip", sa.String(length=64), nullable=True),
        )
    if "observed_pppoe_status" not in columns:
        op.add_column(
            "ont_units",
            sa.Column("observed_pppoe_status", sa.String(length=60), nullable=True),
        )
    if "observed_lan_mode" not in columns:
        op.add_column(
            "ont_units",
            sa.Column("observed_lan_mode", sa.String(length=60), nullable=True),
        )
    if "observed_wifi_clients" not in columns:
        op.add_column(
            "ont_units", sa.Column("observed_wifi_clients", sa.Integer(), nullable=True)
        )
    if "observed_lan_hosts" not in columns:
        op.add_column(
            "ont_units", sa.Column("observed_lan_hosts", sa.Integer(), nullable=True)
        )
    if "observed_runtime_updated_at" not in columns:
        op.add_column(
            "ont_units",
            sa.Column(
                "observed_runtime_updated_at", sa.DateTime(timezone=True), nullable=True
            ),
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = {c["name"] for c in inspector.get_columns("ont_units")}

    for col in [
        "observed_runtime_updated_at",
        "observed_lan_hosts",
        "observed_wifi_clients",
        "observed_lan_mode",
        "observed_pppoe_status",
        "observed_wan_ip",
        "mac_address",
    ]:
        if col in columns:
            op.drop_column("ont_units", col)
