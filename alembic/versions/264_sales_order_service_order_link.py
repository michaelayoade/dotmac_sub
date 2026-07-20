"""Link provisioning service orders to their originating sales lines.

Revision ID: 264_sales_order_service_order_link
Revises: 263_ont_assignment_subscription
"""

import sqlalchemy as sa

from alembic import op

revision = "264_sales_order_service_order_link"
down_revision = "263_ont_assignment_subscription"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "access_credentials",
        sa.Column("subscription_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_access_credentials_subscription_id",
        "access_credentials",
        "subscriptions",
        ["subscription_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_access_credentials_subscription_id",
        "access_credentials",
        ["subscription_id"],
    )
    op.add_column(
        "subscriber_additional_routes",
        sa.Column("subscription_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_subscriber_additional_routes_subscription_id",
        "subscriber_additional_routes",
        "subscriptions",
        ["subscription_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_subscriber_additional_routes_subscription_id",
        "subscriber_additional_routes",
        ["subscription_id"],
    )
    # Bind legacy subscriber-scoped access state only when ownership is
    # unambiguous. Multi-service subscribers remain NULL for operator review.
    op.execute(
        """
        UPDATE access_credentials AS target
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
    op.execute(
        """
        UPDATE subscriber_additional_routes AS target
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
    op.add_column(
        "service_orders",
        sa.Column("sales_order_id", sa.UUID(), nullable=True),
    )
    op.add_column(
        "service_orders",
        sa.Column("sales_order_line_id", sa.UUID(), nullable=True),
    )
    op.add_column(
        "service_orders",
        sa.Column("execution_context", sa.JSON(), nullable=True),
    )
    op.create_foreign_key(
        "fk_service_orders_sales_order_id",
        "service_orders",
        "sales_orders",
        ["sales_order_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_service_orders_sales_order_line_id",
        "service_orders",
        "sales_order_lines",
        ["sales_order_line_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_service_orders_sales_order_id",
        "service_orders",
        ["sales_order_id"],
    )
    op.create_index(
        "uq_service_orders_sales_order_line_id",
        "service_orders",
        ["sales_order_line_id"],
        unique=True,
        postgresql_where=sa.text("sales_order_line_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_column("service_orders", "execution_context")
    op.drop_index(
        "uq_service_orders_sales_order_line_id",
        table_name="service_orders",
    )
    op.drop_index("ix_service_orders_sales_order_id", table_name="service_orders")
    op.drop_constraint(
        "fk_service_orders_sales_order_line_id",
        "service_orders",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_service_orders_sales_order_id",
        "service_orders",
        type_="foreignkey",
    )
    op.drop_column("service_orders", "sales_order_line_id")
    op.drop_column("service_orders", "sales_order_id")
    op.drop_index(
        "ix_subscriber_additional_routes_subscription_id",
        table_name="subscriber_additional_routes",
    )
    op.drop_constraint(
        "fk_subscriber_additional_routes_subscription_id",
        "subscriber_additional_routes",
        type_="foreignkey",
    )
    op.drop_column("subscriber_additional_routes", "subscription_id")
    op.drop_index(
        "ix_access_credentials_subscription_id",
        table_name="access_credentials",
    )
    op.drop_constraint(
        "fk_access_credentials_subscription_id",
        "access_credentials",
        type_="foreignkey",
    )
    op.drop_column("access_credentials", "subscription_id")
