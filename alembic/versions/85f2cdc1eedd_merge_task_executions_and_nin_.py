"""merge_task_executions_and_nin_verifications

Revision ID: 85f2cdc1eedd
Revises: 027_add_subscriber_nin_verifications, 033_add_olt_draining_status
Create Date: 2026-04-17 16:38:27.429878

"""

from alembic import op
import sqlalchemy as sa


revision = '85f2cdc1eedd'
down_revision = ('027_add_subscriber_nin_verifications', '033_add_olt_draining_status')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
