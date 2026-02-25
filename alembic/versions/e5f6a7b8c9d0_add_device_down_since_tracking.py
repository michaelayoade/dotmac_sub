"""add_device_down_since_tracking

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-02-25 19:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e5f6a7b8c9d0"
down_revision: str | None = "d4e5f6a7b8c9"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = [c["name"] for c in inspector.get_columns("network_devices")]

    if "ping_down_since" not in columns:
        op.add_column(
            "network_devices",
            sa.Column("ping_down_since", sa.DateTime(timezone=True), nullable=True),
        )
    if "snmp_down_since" not in columns:
        op.add_column(
            "network_devices",
            sa.Column("snmp_down_since", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = [c["name"] for c in inspector.get_columns("network_devices")]

    if "snmp_down_since" in columns:
        op.drop_column("network_devices", "snmp_down_since")
    if "ping_down_since" in columns:
        op.drop_column("network_devices", "ping_down_since")
