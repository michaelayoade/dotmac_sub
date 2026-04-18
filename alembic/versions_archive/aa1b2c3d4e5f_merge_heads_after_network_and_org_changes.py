"""Merge current Alembic heads.

Revision ID: aa1b2c3d4e5f
Revises: d4e5f6a7b8c0, f2a3b4c5d6e8
Create Date: 2026-03-23 16:45:00.000000
"""

from collections.abc import Sequence

revision: str = "aa1b2c3d4e5f"
down_revision: str | Sequence[str] | None = ("d4e5f6a7b8c0", "f2a3b4c5d6e8")
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
