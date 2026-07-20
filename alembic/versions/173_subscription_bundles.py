"""subscription bundles

Adds ``subscription_bundles`` (multi-service bundle grouping for a
subscriber's subscriptions) and ``subscriptions.bundle_id`` (nullable FK
back to the owning bundle). Mirrors the ``SubscriptionBundle`` ORM model
added in Task 1 (app/models/catalog.py).

Revision ID: 173_subscription_bundles
Revises: 90a03dc0c609
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "173_subscription_bundles"
down_revision = "199_add_nas_mikrotik_api_port"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "subscription_bundles",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "subscriber_id",
            UUID(as_uuid=True),
            sa.ForeignKey("subscribers.id"),
            nullable=False,
        ),
        sa.Column("label", sa.String(160), nullable=True),
        sa.Column(
            "anchor_subscription_id",
            UUID(as_uuid=True),
            sa.ForeignKey("subscriptions.id"),
            nullable=True,
        ),
        sa.Column("is_dedicated", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("status", sa.String(32), server_default="active", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_subscription_bundles_subscriber_id",
        "subscription_bundles",
        ["subscriber_id"],
    )
    op.add_column(
        "subscriptions",
        sa.Column(
            "bundle_id",
            UUID(as_uuid=True),
            sa.ForeignKey("subscription_bundles.id"),
            nullable=True,
        ),
    )
    op.create_index("ix_subscriptions_bundle_id", "subscriptions", ["bundle_id"])


def downgrade() -> None:
    op.drop_index("ix_subscriptions_bundle_id", table_name="subscriptions")
    op.drop_column("subscriptions", "bundle_id")
    op.drop_index(
        "ix_subscription_bundles_subscriber_id",
        table_name="subscription_bundles",
    )
    op.drop_table("subscription_bundles")
