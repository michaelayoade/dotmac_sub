"""add_account_start_date_to_subscribers

Revision ID: 8f802e49c452
Revises: a1b2c3d4e5f6
Create Date: 2026-01-15 13:19:56.326781

"""

from alembic import op
import sqlalchemy as sa

revision = '8f802e49c452'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("subscribers")}
    if "account_start_date" not in columns:
        op.add_column('subscribers', sa.Column('account_start_date', sa.DateTime(timezone=True), nullable=True))
    # Backfill existing records with created_at value
    op.execute("UPDATE subscribers SET account_start_date = created_at WHERE account_start_date IS NULL")


def downgrade() -> None:
    op.drop_column('subscribers', 'account_start_date')
