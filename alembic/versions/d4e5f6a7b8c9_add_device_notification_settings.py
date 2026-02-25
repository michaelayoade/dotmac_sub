"""add_device_notification_settings

Revision ID: d4e5f6a7b8c9
Revises: c2d3e4f5a6b7
Create Date: 2026-02-25 18:35:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4e5f6a7b8c9"
down_revision: str | None = "c2d3e4f5a6b7"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = [c["name"] for c in inspector.get_columns("network_devices")]

    if "send_notifications" not in columns:
        op.add_column(
            "network_devices",
            sa.Column("send_notifications", sa.Boolean(), nullable=False, server_default=sa.true()),
        )
        op.alter_column("network_devices", "send_notifications", server_default=None)

    if "notification_delay_minutes" not in columns:
        op.add_column(
            "network_devices",
            sa.Column("notification_delay_minutes", sa.Integer(), nullable=False, server_default="0"),
        )
        op.alter_column("network_devices", "notification_delay_minutes", server_default=None)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = [c["name"] for c in inspector.get_columns("network_devices")]

    if "notification_delay_minutes" in columns:
        op.drop_column("network_devices", "notification_delay_minutes")
    if "send_notifications" in columns:
        op.drop_column("network_devices", "send_notifications")
