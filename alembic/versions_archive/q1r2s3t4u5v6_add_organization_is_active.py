"""Add is_active column to organizations table.

Revision ID: q1r2s3t4u5v6
Revises: j1k2l3m4n5o6
Create Date: 2026-03-10
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "q1r2s3t4u5v6"
down_revision = "j1k2l3m4n5o6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [c["name"] for c in inspector.get_columns("organizations")]
    if "is_active" not in columns:
        op.add_column(
            "organizations",
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [c["name"] for c in inspector.get_columns("organizations")]
    if "is_active" in columns:
        op.drop_column("organizations", "is_active")
