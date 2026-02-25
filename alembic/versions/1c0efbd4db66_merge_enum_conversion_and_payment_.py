"""merge enum conversion and payment provider branches

Revision ID: 1c0efbd4db66
Revises: e8a1c4d2f7b9, v2w3x4y5z6a7
Create Date: 2026-02-22 17:45:09.107037

"""

from alembic import op
import sqlalchemy as sa


revision = '1c0efbd4db66'
down_revision = ('e8a1c4d2f7b9', 'v2w3x4y5z6a7')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
