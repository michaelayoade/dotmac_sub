"""add_nas_connection_rules

Revision ID: b1c2d3e4f5a6
Revises: a9b8c7d6e5f4
Create Date: 2026-02-25 15:35:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b1c2d3e4f5a6"
down_revision: str | None = "a9b8c7d6e5f4"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if not inspector.has_table("nas_connection_rules"):
        op.create_table(
            "nas_connection_rules",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("nas_device_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column(
                "connection_type",
                sa.Enum("pppoe", "dhcp", "ipoe", "static", "hotspot", name="connectiontype"),
                nullable=True,
            ),
            sa.Column("ip_assignment_mode", sa.String(length=40), nullable=True),
            sa.Column("rate_limit_profile", sa.String(length=120), nullable=True),
            sa.Column("match_expression", sa.String(length=255), nullable=True),
            sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["nas_device_id"], ["nas_devices.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("nas_device_id", "name", name="uq_nas_connection_rules_device_name"),
        )
        op.create_index(
            "ix_nas_connection_rules_device_active",
            "nas_connection_rules",
            ["nas_device_id", "is_active"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if inspector.has_table("nas_connection_rules"):
        op.drop_index("ix_nas_connection_rules_device_active", table_name="nas_connection_rules")
        op.drop_table("nas_connection_rules")
