"""merge migration heads

Revision ID: 147533de6996
Revises: 64e6fb6f4b38, x5y6z7a8b9c0
Create Date: 2026-03-07 08:04:32.499543

"""

from alembic import op
import sqlalchemy as sa


revision = '147533de6996'
down_revision = ('64e6fb6f4b38', 'x5y6z7a8b9c0')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
