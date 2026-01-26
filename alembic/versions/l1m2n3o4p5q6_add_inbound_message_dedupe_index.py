"""Add inbound CRM message dedupe index for email and Meta DMs.

Revision ID: l1m2n3o4p5q6
Revises: k2l3m4n5o6p7
Create Date: 2026-01-22
"""

from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision = "l1m2n3o4p5q6"
down_revision = "k2l3m4n5o6p7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_crm_messages_inbound_external",
        "crm_messages",
        ["channel_type", "external_id"],
        unique=True,
        sqlite_where=text(
            "external_id IS NOT NULL "
            "AND direction = 'inbound' "
            "AND channel_type IN ('email', 'facebook_messenger', 'instagram_dm')"
        ),
        postgresql_where=text(
            "external_id IS NOT NULL "
            "AND direction = 'inbound' "
            "AND channel_type IN ('email', 'facebook_messenger', 'instagram_dm')"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_crm_messages_inbound_external",
        table_name="crm_messages",
    )
