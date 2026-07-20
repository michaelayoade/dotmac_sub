"""Bind ONT assignments to exact catalog subscriptions.

Revision ID: 263_ont_assignment_subscription
Revises: 262_huawei_ont_observations
"""

import sqlalchemy as sa

from alembic import op

revision = "263_ont_assignment_subscription"
down_revision = "262_huawei_ont_observations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ont_assignments",
        sa.Column("subscription_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_ont_assignments_subscription_id",
        "ont_assignments",
        "subscriptions",
        ["subscription_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_ont_assignments_subscription_id",
        "ont_assignments",
        ["subscription_id"],
    )
    # Backfill only active assignments whose subscriber has exactly one active
    # subscription. Ambiguous multi-service subscribers remain NULL for review.
    op.execute(
        """
        UPDATE ont_assignments AS oa
        SET subscription_id = candidate.subscription_id
        FROM (
            SELECT subscription.subscriber_id,
                   subscription.id AS subscription_id
            FROM subscriptions AS subscription
            WHERE subscription.status = 'active'
              AND NOT EXISTS (
                  SELECT 1
                  FROM subscriptions AS other
                  WHERE other.subscriber_id = subscription.subscriber_id
                    AND other.status = 'active'
                    AND other.id <> subscription.id
              )
        ) AS candidate
        WHERE oa.subscriber_id = candidate.subscriber_id
          AND oa.is_active = true
          AND oa.subscription_id IS NULL
        """
    )


def downgrade() -> None:
    op.drop_index("ix_ont_assignments_subscription_id", table_name="ont_assignments")
    op.drop_constraint(
        "fk_ont_assignments_subscription_id",
        "ont_assignments",
        type_="foreignkey",
    )
    op.drop_column("ont_assignments", "subscription_id")
