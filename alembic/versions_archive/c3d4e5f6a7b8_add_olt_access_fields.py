"""Add SSH and NETCONF access fields to OLT devices.

Revision ID: c3d4e5f6a7b8
Revises: y5z6a7b8c9d0
Create Date: 2026-03-03
"""

import sqlalchemy as sa

from alembic import op

revision = "c3d4e5f6a7b8"
down_revision = "y5z6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_columns = {
        column["name"] for column in inspector.get_columns("olt_devices")
    }

    if "ssh_username" not in existing_columns:
        op.add_column(
            "olt_devices",
            sa.Column("ssh_username", sa.String(length=120), nullable=True),
        )
    if "ssh_password" not in existing_columns:
        op.add_column(
            "olt_devices",
            sa.Column("ssh_password", sa.String(length=255), nullable=True),
        )
    if "ssh_port" not in existing_columns:
        op.add_column(
            "olt_devices",
            sa.Column(
                "ssh_port", sa.Integer(), nullable=True, server_default=sa.text("22")
            ),
        )
    if "netconf_enabled" not in existing_columns:
        op.add_column(
            "olt_devices",
            sa.Column(
                "netconf_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )
    if "netconf_port" not in existing_columns:
        op.add_column(
            "olt_devices",
            sa.Column(
                "netconf_port",
                sa.Integer(),
                nullable=True,
                server_default=sa.text("830"),
            ),
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_columns = {
        column["name"] for column in inspector.get_columns("olt_devices")
    }

    if "netconf_port" in existing_columns:
        op.drop_column("olt_devices", "netconf_port")
    if "netconf_enabled" in existing_columns:
        op.drop_column("olt_devices", "netconf_enabled")
    if "ssh_port" in existing_columns:
        op.drop_column("olt_devices", "ssh_port")
    if "ssh_password" in existing_columns:
        op.drop_column("olt_devices", "ssh_password")
    if "ssh_username" in existing_columns:
        op.drop_column("olt_devices", "ssh_username")
