"""merge_multiple_heads_jan2026

Revision ID: 3c20a91bc334
Revises: 2efa114cd8e6, 3f2a1b4c5d6e, c8d9e0f1a2b3, j8k9l0m1n2o3
Create Date: 2026-01-20 07:01:13.528009

"""

from alembic import op
import sqlalchemy as sa


revision = '3c20a91bc334'
down_revision = ('2efa114cd8e6', '3f2a1b4c5d6e', 'c8d9e0f1a2b3', 'j8k9l0m1n2o3')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
