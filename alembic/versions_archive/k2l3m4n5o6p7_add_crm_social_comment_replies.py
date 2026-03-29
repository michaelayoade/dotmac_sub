"""Add CRM social comment replies table.

Revision ID: k2l3m4n5o6p7
Revises: k1l2m3n4o5p6
Create Date: 2026-01-20
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = 'k2l3m4n5o6p7'
down_revision = 'k1l2m3n4o5p6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    comment_platform_enum = postgresql.ENUM(
        "facebook", "instagram", name="socialcommentplatform", create_type=False
    )
    op.create_table(
        'crm_social_comment_replies',
        sa.Column('id', sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('comment_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('platform', comment_platform_enum, nullable=False),
        sa.Column('external_id', sa.String(length=200)),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('created_time', sa.DateTime(timezone=True)),
        sa.Column('raw_payload', sa.dialects.postgresql.JSON()),
        sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.UniqueConstraint('platform', 'external_id', name='uq_crm_social_comment_replies_platform_external'),
    )


def downgrade() -> None:
    op.drop_table('crm_social_comment_replies')
