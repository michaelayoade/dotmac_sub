"""Add default-off AI inbox automation policy fields.

Revision ID: 409_ai_inbox_automation_dormant_policy
Revises: 408_radius_session_latest_projection
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "409_ai_inbox_automation_dormant_policy"
down_revision = "408_radius_session_latest_projection"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ai_intake_configs",
        sa.Column(
            "auto_reply_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "ai_intake_configs",
        sa.Column("workflow_steps", sa.JSON(), nullable=True),
    )
    op.add_column(
        "ai_intake_configs",
        sa.Column("context_sources", sa.JSON(), nullable=True),
    )
    op.add_column(
        "ai_intake_configs",
        sa.Column(
            "auto_handoff_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "ai_intake_configs",
        sa.Column(
            "handoff_policy",
            sa.String(length=40),
            nullable=False,
            server_default="manual_review",
        ),
    )
    op.add_column(
        "ai_intake_configs",
        sa.Column(
            "assignment_strategy",
            sa.String(length=40),
            nullable=False,
            server_default="available_round_robin",
        ),
    )
    op.alter_column("ai_intake_configs", "auto_reply_enabled", server_default=None)
    op.alter_column("ai_intake_configs", "auto_handoff_enabled", server_default=None)
    op.alter_column("ai_intake_configs", "handoff_policy", server_default=None)
    op.alter_column("ai_intake_configs", "assignment_strategy", server_default=None)


def downgrade() -> None:
    op.drop_column("ai_intake_configs", "assignment_strategy")
    op.drop_column("ai_intake_configs", "handoff_policy")
    op.drop_column("ai_intake_configs", "auto_handoff_enabled")
    op.drop_column("ai_intake_configs", "context_sources")
    op.drop_column("ai_intake_configs", "workflow_steps")
    op.drop_column("ai_intake_configs", "auto_reply_enabled")
