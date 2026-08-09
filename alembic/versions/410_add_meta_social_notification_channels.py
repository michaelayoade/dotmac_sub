"""Add Meta social notification channels.

Revision ID: 410_add_meta_social_notification_channels
Revises: 409_ai_inbox_automation_dormant_policy
Create Date: 2026-08-09 00:00:00.000000
"""

from __future__ import annotations

from alembic import op

revision = "410_add_meta_social_notification_channels"
down_revision = "409_ai_inbox_automation_dormant_policy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TYPE notificationchannel ADD VALUE IF NOT EXISTS 'facebook_messenger'"
    )
    op.execute("ALTER TYPE notificationchannel ADD VALUE IF NOT EXISTS 'instagram_dm'")


def downgrade() -> None:
    # PostgreSQL enum values cannot be removed safely without rewriting every
    # dependent column. The forward migration is additive and backward compatible.
    pass
