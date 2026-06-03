"""Add subscriber ownership fields to customer notifications.

Revision ID: 106_add_subscriber_owned_notifications
Revises: 105_add_pending_ont_provisioning_statuses
Create Date: 2026-05-25
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "106_add_subscriber_owned_notifications"
down_revision = "105_add_pending_ont_provisioning_statuses"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid_type = postgresql.UUID(as_uuid=True) if op.get_bind().dialect.name == "postgresql" else sa.String(length=36)

    op.add_column(
        "notifications",
        sa.Column("subscriber_id", uuid_type, sa.ForeignKey("subscribers.id", ondelete="SET NULL"), nullable=True),
    )
    op.add_column(
        "notifications",
        sa.Column("event_type", sa.String(length=120), nullable=True),
    )
    op.add_column(
        "notifications",
        sa.Column("category", sa.String(length=40), nullable=True),
    )
    op.create_index(
        "ix_notifications_subscriber_id",
        "notifications",
        ["subscriber_id"],
        unique=False,
    )
    op.create_index(
        "ix_notifications_event_type",
        "notifications",
        ["event_type"],
        unique=False,
    )
    op.create_index(
        "ix_notifications_category",
        "notifications",
        ["category"],
        unique=False,
    )

    op.add_column(
        "customer_notification_events",
        sa.Column("subscriber_id", uuid_type, sa.ForeignKey("subscribers.id", ondelete="SET NULL"), nullable=True),
    )
    op.create_index(
        "ix_customer_notification_events_subscriber_id",
        "customer_notification_events",
        ["subscriber_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_customer_notification_events_subscriber_id",
        table_name="customer_notification_events",
    )
    op.drop_column("customer_notification_events", "subscriber_id")

    op.drop_index("ix_notifications_category", table_name="notifications")
    op.drop_index("ix_notifications_event_type", table_name="notifications")
    op.drop_index("ix_notifications_subscriber_id", table_name="notifications")
    op.drop_column("notifications", "category")
    op.drop_column("notifications", "event_type")
    op.drop_column("notifications", "subscriber_id")
