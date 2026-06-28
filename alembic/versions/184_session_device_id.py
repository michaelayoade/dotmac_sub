"""add device_id to sessions for per-device session replace

Revision ID: 184_session_device_id
Revises: 183_admin_infrastructure_alerts
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "184_session_device_id"
down_revision = "183_admin_infrastructure_alerts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column("device_id", sa.String(length=64), nullable=True),
    )
    op.create_index("ix_sessions_device_id", "sessions", ["device_id"])


def downgrade() -> None:
    op.drop_index("ix_sessions_device_id", table_name="sessions")
    op.drop_column("sessions", "device_id")
