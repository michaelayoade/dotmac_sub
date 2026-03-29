"""Add ONT sync tracking columns (last_sync_source, last_sync_at).

Revision ID: e1f2a3b4c5d6
Revises: z7b8c9d0e1f2
Create Date: 2026-03-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision: str = "e1f2a3b4c5d6"
down_revision: str | Sequence[str] | None = "z7b8c9d0e1f2"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = inspect(conn)
    columns = [c["name"] for c in inspector.get_columns("ont_units")]

    if "last_sync_source" not in columns:
        op.add_column(
            "ont_units",
            sa.Column("last_sync_source", sa.String(40), nullable=True),
        )
    if "last_sync_at" not in columns:
        op.add_column(
            "ont_units",
            sa.Column(
                "last_sync_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = inspect(conn)
    columns = [c["name"] for c in inspector.get_columns("ont_units")]

    if "last_sync_at" in columns:
        op.drop_column("ont_units", "last_sync_at")
    if "last_sync_source" in columns:
        op.drop_column("ont_units", "last_sync_source")
