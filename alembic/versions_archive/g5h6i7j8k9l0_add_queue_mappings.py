"""Add queue_mappings table for bandwidth monitoring

Revision ID: g5h6i7j8k9l0
Revises: f4a1b2c3d5e6
Create Date: 2026-01-18

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'g5h6i7j8k9l0'
down_revision: Union[str, None] = 'f4a1b2c3d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Check if table already exists
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if 'queue_mappings' not in existing_tables:
        op.create_table(
            'queue_mappings',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
            sa.Column('nas_device_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('nas_devices.id'), nullable=False),
            sa.Column('queue_name', sa.String(255), nullable=False),
            sa.Column('subscription_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('subscriptions.id'), nullable=False),
            sa.Column('is_active', sa.Boolean(), default=True, nullable=False, server_default='true'),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
            sa.UniqueConstraint('nas_device_id', 'queue_name', name='uq_queue_mappings_device_queue'),
        )
        op.create_index('ix_queue_mappings_subscription_id', 'queue_mappings', ['subscription_id'])
        op.create_index('ix_queue_mappings_nas_device_id', 'queue_mappings', ['nas_device_id'])


def downgrade() -> None:
    op.drop_index('ix_queue_mappings_nas_device_id', table_name='queue_mappings')
    op.drop_index('ix_queue_mappings_subscription_id', table_name='queue_mappings')
    op.drop_table('queue_mappings')
