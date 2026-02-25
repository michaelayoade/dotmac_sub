"""Add Option 82 and connection type fields to access credentials.

Revision ID: a1b2c3d4e5f6
Revises: z7b8c9d0e1f2
Create Date: 2026-02-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = "z7b8c9d0e1f2"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = inspect(conn)
    columns = {c["name"] for c in inspector.get_columns("access_credentials")}

    if "circuit_id" not in columns:
        op.add_column(
            "access_credentials",
            sa.Column("circuit_id", sa.String(255), nullable=True),
        )
    if "remote_id" not in columns:
        op.add_column(
            "access_credentials",
            sa.Column("remote_id", sa.String(255), nullable=True),
        )
    if "connection_type" not in columns:
        # Reuse existing connectiontype enum
        connection_type_enum = sa.Enum(
            "pppoe", "dhcp", "ipoe", "static", "hotspot",
            name="connectiontype",
            create_type=False,
        )
        op.add_column(
            "access_credentials",
            sa.Column("connection_type", connection_type_enum, nullable=True),
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = inspect(conn)
    columns = {c["name"] for c in inspector.get_columns("access_credentials")}

    if "connection_type" in columns:
        op.drop_column("access_credentials", "connection_type")
    if "remote_id" in columns:
        op.drop_column("access_credentials", "remote_id")
    if "circuit_id" in columns:
        op.drop_column("access_credentials", "circuit_id")
