"""add_person_permissions_table

Revision ID: 8414d637b1fb
Revises: 69928eb6e61f
Create Date: 2026-01-13 05:57:49.806615

"""

from alembic import op
import sqlalchemy as sa


revision = '8414d637b1fb'
down_revision = '69928eb6e61f'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'person_permissions',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('person_id', sa.UUID(), nullable=False),
        sa.Column('permission_id', sa.UUID(), nullable=False),
        sa.Column('granted_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('granted_by_person_id', sa.UUID(), nullable=True),
        sa.ForeignKeyConstraint(['granted_by_person_id'], ['people.id']),
        sa.ForeignKeyConstraint(['permission_id'], ['permissions.id']),
        sa.ForeignKeyConstraint(['person_id'], ['people.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'person_id', 'permission_id', name='uq_person_permissions_person_permission'
        ),
    )
    op.create_index(
        'idx_person_permissions_person_id', 'person_permissions', ['person_id']
    )


def downgrade() -> None:
    op.drop_index('idx_person_permissions_person_id', table_name='person_permissions')
    op.drop_table('person_permissions')
