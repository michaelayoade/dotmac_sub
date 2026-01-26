"""Add work order fields for field service optimization.

Revision ID: i7j8k9l0m1n2
Revises: h6i7j8k9l0m1
Create Date: 2024-01-16 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'i7j8k9l0m1n2'
down_revision = 'h6i7j8k9l0m1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add field service optimization columns to work_orders
    op.add_column('work_orders', sa.Column('required_skills', postgresql.JSON(), nullable=True))
    op.add_column('work_orders', sa.Column('estimated_duration_minutes', sa.Integer(), nullable=True))
    op.add_column('work_orders', sa.Column('estimated_arrival_at', sa.DateTime(timezone=True), nullable=True))

    # Create index for better query performance on estimated arrival
    op.create_index('ix_work_orders_estimated_arrival_at', 'work_orders', ['estimated_arrival_at'])


def downgrade() -> None:
    op.drop_index('ix_work_orders_estimated_arrival_at', table_name='work_orders')
    op.drop_column('work_orders', 'estimated_arrival_at')
    op.drop_column('work_orders', 'estimated_duration_minutes')
    op.drop_column('work_orders', 'required_skills')
