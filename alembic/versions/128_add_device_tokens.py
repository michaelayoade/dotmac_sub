"""Add device_tokens for mobile push (FCM/APNs).

Revision ID: 128_add_device_tokens
Revises: 127_add_addon_validity_days
Create Date: 2026-06-09
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "128_add_device_tokens"
down_revision = "127_add_addon_validity_days"
branch_labels = None
depends_on = None

_TABLE = "device_tokens"


def upgrade() -> None:
    bind = op.get_bind()
    # SQLite (tests) builds the schema from model metadata via create_all.
    if bind.dialect.name == "sqlite":
        return
    inspector = inspect(bind)
    if _TABLE in inspector.get_table_names():
        return
    op.create_table(
        _TABLE,
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "subscriber_id",
            UUID(as_uuid=True),
            sa.ForeignKey("subscribers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token", sa.String(length=512), nullable=False, unique=True),
        sa.Column("platform", sa.String(length=16), nullable=True),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_device_tokens_subscriber_id", _TABLE, ["subscriber_id"]
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    inspector = inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return
    op.drop_index("ix_device_tokens_subscriber_id", table_name=_TABLE)
    op.drop_table(_TABLE)
