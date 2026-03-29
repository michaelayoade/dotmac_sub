"""Add modules to settingdomain enum.

Revision ID: n4p5q6r7s8t9
Revises: b1c2d3e4f5a7, c0d1e2f3a4b5, z7b8c9d0e1f2
Create Date: 2026-02-25 10:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "n4p5q6r7s8t9"
down_revision = ("b1c2d3e4f5a7", "c0d1e2f3a4b5", "z7b8c9d0e1f2")
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("ALTER TYPE settingdomain ADD VALUE IF NOT EXISTS 'modules'"))


def downgrade() -> None:
    # PostgreSQL enums do not support removing individual values safely.
    pass
