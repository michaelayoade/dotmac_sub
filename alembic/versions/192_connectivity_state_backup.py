"""Add connectivity_state_backups (pre-change backup of subscriber connectivity).

Captures a subscriber's external RADIUS rows + credential flags + IP state
before a destructive connectivity mutation, so a bad convergence is auditable
and restorable. Additive only; no enum types (plain columns + JSON), so it
applies cleanly as the app DB user.

Revision ID: 192_connectivity_state_backup
Revises: 191_add_quote_mirror
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "192_connectivity_state_backup"
down_revision = "191_add_quote_mirror"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "connectivity_state_backups",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "subscriber_id",
            UUID(as_uuid=True),
            sa.ForeignKey("subscribers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("reason", sa.String(length=40), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("captured_by", sa.String(length=64), nullable=True),
        sa.Column("radcheck", sa.JSON(), nullable=True),
        sa.Column("radreply", sa.JSON(), nullable=True),
        sa.Column("credentials", sa.JSON(), nullable=True),
        sa.Column("ip_state", sa.JSON(), nullable=True),
        sa.Column("restored_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("restored_by", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_connectivity_state_backups_subscriber_id",
        "connectivity_state_backups",
        ["subscriber_id"],
    )
    op.create_index(
        "ix_connectivity_state_backups_reason",
        "connectivity_state_backups",
        ["reason"],
    )
    op.create_index(
        "ix_connectivity_state_backups_created_at",
        "connectivity_state_backups",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_connectivity_state_backups_created_at",
        table_name="connectivity_state_backups",
    )
    op.drop_index(
        "ix_connectivity_state_backups_reason",
        table_name="connectivity_state_backups",
    )
    op.drop_index(
        "ix_connectivity_state_backups_subscriber_id",
        table_name="connectivity_state_backups",
    )
    op.drop_table("connectivity_state_backups")
