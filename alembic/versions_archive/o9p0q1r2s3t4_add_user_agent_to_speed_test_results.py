"""Add user agent column to speed test results.

Revision ID: o9p0q1r2s3t4
Revises: n4p5q6r7s8t9
Create Date: 2026-02-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision: str = "o9p0q1r2s3t4"
down_revision: str | Sequence[str] | None = "n4p5q6r7s8t9"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table("speed_test_results"):
        return
    columns = {c["name"] for c in inspector.get_columns("speed_test_results")}
    if "user_agent" not in columns:
        op.add_column(
            "speed_test_results",
            sa.Column("user_agent", sa.String(length=500), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table("speed_test_results"):
        return
    columns = {c["name"] for c in inspector.get_columns("speed_test_results")}
    if "user_agent" in columns:
        op.drop_column("speed_test_results", "user_agent")
