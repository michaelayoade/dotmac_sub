"""add_parent_device_to_network_devices

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-02-25 18:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c2d3e4f5a6b7"
down_revision: str | None = "b1c2d3e4f5a6"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = [c["name"] for c in inspector.get_columns("network_devices")]

    if "parent_device_id" not in columns:
        op.add_column(
            "network_devices",
            sa.Column("parent_device_id", postgresql.UUID(as_uuid=True), nullable=True),
        )
        op.create_foreign_key(
            "fk_network_devices_parent_device_id",
            "network_devices",
            "network_devices",
            ["parent_device_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_index(
            "ix_network_devices_parent_device_id",
            "network_devices",
            ["parent_device_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = [c["name"] for c in inspector.get_columns("network_devices")]

    if "parent_device_id" in columns:
        op.drop_index("ix_network_devices_parent_device_id", table_name="network_devices")
        op.drop_constraint(
            "fk_network_devices_parent_device_id",
            "network_devices",
            type_="foreignkey",
        )
        op.drop_column("network_devices", "parent_device_id")
