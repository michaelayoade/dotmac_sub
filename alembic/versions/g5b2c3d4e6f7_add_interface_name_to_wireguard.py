"""Add interface_name to wireguard_servers.

Revision ID: g5b2c3d4e6f7
Revises: f4a1b2c3d5e6
Create Date: 2025-01-18

Adds the interface_name column to wireguard_servers for tracking
the Linux interface name (e.g., wg0, wg-infra) on the VPS.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "g5b2c3d4e6f7"
down_revision = "g5h6i7j8k9l0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add interface_name column to wireguard_servers
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    existing_columns = {col["name"] for col in inspector.get_columns("wireguard_servers")}

    if "interface_name" not in existing_columns:
        op.add_column(
            "wireguard_servers",
            sa.Column(
                "interface_name",
                sa.String(32),
                nullable=False,
                server_default="wg0"
            )
        )


def downgrade() -> None:
    op.drop_column("wireguard_servers", "interface_name")
