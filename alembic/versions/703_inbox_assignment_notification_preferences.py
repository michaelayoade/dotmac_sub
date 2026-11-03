"""add per-agent Team Inbox assignment notification preferences

Revision ID: 703_inbox_assignment_notification_preferences
Revises: 703_ip_pool_labels
Create Date: 2026-11-03
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "703_inbox_assignment_notification_preferences"
down_revision: str | None = "703_ip_pool_labels"
branch_labels = None
depends_on = None

_TABLE = "inbox_agent_introduction_preferences"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(
            "assignment_in_app_notifications",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "assignment_email_notifications",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )


def downgrade() -> None:
    op.drop_column(_TABLE, "assignment_email_notifications")
    op.drop_column(_TABLE, "assignment_in_app_notifications")
