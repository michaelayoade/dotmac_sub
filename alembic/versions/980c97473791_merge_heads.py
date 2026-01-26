"""Merge heads

Revision ID: 980c97473791
Revises: 8414d637b1fb, 8d6e4b1de8d8
Create Date: 2026-01-13 06:15:07.302119

"""

from alembic import op
import sqlalchemy as sa


revision = '980c97473791'
down_revision = ('8414d637b1fb', '8d6e4b1de8d8')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
