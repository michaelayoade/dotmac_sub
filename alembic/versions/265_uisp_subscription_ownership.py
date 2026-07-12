"""Bind UISP CPE inventory to exact subscriptions.

Revision ID: 265_uisp_subscription_ownership
Revises: 264_sales_order_service_order_link
"""

import sqlalchemy as sa

from alembic import op

revision = "265_uisp_subscription_ownership"
down_revision = "264_sales_order_service_order_link"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("cpe_devices", sa.Column("subscription_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_cpe_devices_subscription_id",
        "cpe_devices",
        "subscriptions",
        ["subscription_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_cpe_devices_subscription_id", "cpe_devices", ["subscription_id"]
    )
    op.execute(
        """
        UPDATE cpe_devices AS target
        SET subscription_id = candidate.subscription_id
        FROM (
            SELECT subscription.subscriber_id,
                   subscription.id AS subscription_id
            FROM subscriptions AS subscription
            WHERE subscription.status IN ('pending', 'active')
              AND NOT EXISTS (
                  SELECT 1
                  FROM subscriptions AS other
                  WHERE other.subscriber_id = subscription.subscriber_id
                    AND other.status IN ('pending', 'active')
                    AND other.id <> subscription.id
              )
        ) AS candidate
        WHERE target.subscriber_id = candidate.subscriber_id
          AND target.subscription_id IS NULL
        """
    )


def downgrade() -> None:
    op.drop_index("ix_cpe_devices_subscription_id", table_name="cpe_devices")
    op.drop_constraint(
        "fk_cpe_devices_subscription_id", "cpe_devices", type_="foreignkey"
    )
    op.drop_column("cpe_devices", "subscription_id")
