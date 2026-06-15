"""Add read tracking for customer notifications.

Revision ID: 151_customer_notification_read_at
Revises: 152_subscriber_additional_routes
Create Date: 2026-06-15

Re-parented onto 152 (main's head) instead of 150: this migration was orphaned
when prod moved to a main-current branch, and 152 also chains off 150, so
keeping 151 on 150 would create two alembic heads. Re-chaining it linearly
avoids the multi-head deploy stall. The add_column ops are guarded by
``_has_column``, so this is a clean no-op against the prod DB where the columns
already exist.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "151_customer_notification_read_at"
down_revision = "152_subscriber_additional_routes"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = inspect(op.get_bind())
    return any(
        column["name"] == column_name for column in inspector.get_columns(table_name)
    )


def upgrade() -> None:
    if not _has_column("notifications", "read_at"):
        op.add_column(
            "notifications",
            sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        )
    if not _has_column("customer_notification_events", "read_at"):
        op.add_column(
            "customer_notification_events",
            sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    if _has_column("customer_notification_events", "read_at"):
        op.drop_column("customer_notification_events", "read_at")
    if _has_column("notifications", "read_at"):
        op.drop_column("notifications", "read_at")
