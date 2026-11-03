"""add CSAT inbox-channel report evidence

Revision ID: 705_support_csat_report_depth
Revises: 704_inbox_agent_presence_session_leases
Create Date: 2026-11-03
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import text

from alembic import op

revision: str = "705_support_csat_report_depth"
down_revision: str | None = "704_inbox_agent_presence_session_leases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("support_csat_requests", sa.Column("source_channel", sa.String(40)))
    op.create_index(
        "ix_support_csat_channel_resolution",
        "support_csat_requests",
        ["source_channel", "resolution_at"],
    )
    op.execute(
        text(
            """
            UPDATE support_csat_requests AS request
               SET source_channel = conversation.channel_type
              FROM inbox_conversations AS conversation
             WHERE request.source_type = 'inbox_conversation'
               AND request.source_id = conversation.id
               AND request.source_channel IS NULL
            """
        )
    )


def downgrade() -> None:
    op.drop_index(
        "ix_support_csat_channel_resolution", table_name="support_csat_requests"
    )
    op.drop_column("support_csat_requests", "source_channel")
