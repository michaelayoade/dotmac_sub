"""merge_multiple_heads

Revision ID: c39ea6fb3c1e
Revises: 1f2a3c4d5e6f, 7f3c2a9e6c51, 8f3c1b2a9c01
Create Date: 2026-01-14 05:51:17.087894

"""

from alembic import op
import sqlalchemy as sa


revision = 'c39ea6fb3c1e'
down_revision = ('1f2a3c4d5e6f', '7f3c2a9e6c51', '8f3c1b2a9c01')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
