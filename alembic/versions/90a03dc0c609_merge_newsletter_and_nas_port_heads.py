"""merge newsletter and nas-port heads

Revision ID: 90a03dc0c609
Revises: 163_newsletter_subscription_list, 199_add_nas_mikrotik_api_port
Create Date: 2026-07-02 17:18:09.985576

"""

from alembic import op
import sqlalchemy as sa


revision = '90a03dc0c609'
down_revision = ('163_newsletter_subscription_list', '199_add_nas_mikrotik_api_port')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
