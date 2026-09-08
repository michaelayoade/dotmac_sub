"""Add AI intake customer-response timeout state.

Revision ID: 570_ai_intake_customer_response_timeout
Revises: 569_retire_crm_chat_authority
Create Date: 2026-08-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "570_ai_intake_customer_response_timeout"
down_revision: str | None = "569_retire_crm_chat_authority"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_INDEX_NAME = "ix_ai_intake_sessions_customer_wait"


def upgrade() -> None:
    op.add_column(
        "ai_intake_configs",
        sa.Column(
            "customer_response_timeout_minutes",
            sa.Integer(),
            nullable=False,
            server_default="5",
        ),
    )
    op.execute(
        "UPDATE ai_intake_configs "
        "SET customer_response_timeout_minutes = "
        "COALESCE(escalate_after_minutes, 5)"
    )
    op.alter_column(
        "ai_intake_configs",
        "customer_response_timeout_minutes",
        server_default=None,
    )
    op.add_column(
        "ai_intake_sessions",
        sa.Column(
            "customer_wait_started_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.add_column(
        "ai_intake_sessions",
        sa.Column(
            "customer_wait_expires_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.create_index(
        _INDEX_NAME,
        "ai_intake_sessions",
        ["state", "customer_wait_expires_at"],
    )


def downgrade() -> None:
    op.drop_index(_INDEX_NAME, table_name="ai_intake_sessions")
    op.drop_column("ai_intake_sessions", "customer_wait_expires_at")
    op.drop_column("ai_intake_sessions", "customer_wait_started_at")
    op.drop_column("ai_intake_configs", "customer_response_timeout_minutes")
