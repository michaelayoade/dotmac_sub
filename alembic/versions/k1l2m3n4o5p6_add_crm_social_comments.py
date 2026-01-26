"""Add CRM social comments table.

Revision ID: k1l2m3n4o5p6
Revises: j8k9l0m1n2o3
Create Date: 2026-01-20
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'k1l2m3n4o5p6'
down_revision = 'j8k9l0m1n2o3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'crm_social_comments',
        sa.Column('id', sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('platform', sa.Enum('facebook', 'instagram', name='socialcommentplatform'), nullable=False),
        sa.Column('external_id', sa.String(length=200), nullable=False),
        sa.Column('external_post_id', sa.String(length=200)),
        sa.Column('source_account_id', sa.String(length=200)),
        sa.Column('author_id', sa.String(length=200)),
        sa.Column('author_name', sa.String(length=200)),
        sa.Column('message', sa.Text()),
        sa.Column('created_time', sa.DateTime(timezone=True)),
        sa.Column('permalink_url', sa.String(length=500)),
        sa.Column('raw_payload', sa.dialects.postgresql.JSON()),
        sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.UniqueConstraint('platform', 'external_id', name='uq_crm_social_comments_platform_external'),
    )


def downgrade() -> None:
    op.drop_table('crm_social_comments')
    op.execute('DROP TYPE IF EXISTS socialcommentplatform')
