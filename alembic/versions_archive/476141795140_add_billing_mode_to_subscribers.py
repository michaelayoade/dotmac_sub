"""Add billing_mode to subscribers

Revision ID: 476141795140
Revises: 0fe6eb089ea9
Create Date: 2026-03-23 10:52:02.533931

"""

import sqlalchemy as sa
from sqlalchemy import inspect, text

from alembic import op

revision = "476141795140"
down_revision = "0fe6eb089ea9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = inspect(conn)

    # Ensure the billingmode enum type exists (already created by catalog models)
    conn.execute(
        text(
            "DO $$ BEGIN "
            "CREATE TYPE billingmode AS ENUM ('prepaid', 'postpaid'); "
            "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
        )
    )

    existing_columns = [c["name"] for c in inspector.get_columns("subscribers")]
    if "billing_mode" not in existing_columns:
        op.add_column(
            "subscribers",
            sa.Column(
                "billing_mode",
                sa.Enum("prepaid", "postpaid", name="billingmode", create_type=False),
                nullable=False,
                server_default="prepaid",
            ),
        )

        # Backfill from active subscription billing_mode where available
        conn.execute(
            text("""
            UPDATE subscribers s
            SET billing_mode = sub.billing_mode
            FROM (
                SELECT DISTINCT ON (subscriber_id)
                    subscriber_id, billing_mode
                FROM subscriptions
                WHERE status = 'active'
                ORDER BY subscriber_id, created_at DESC
            ) sub
            WHERE s.id = sub.subscriber_id
              AND s.billing_mode = 'prepaid'
        """)
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = inspect(conn)
    existing_columns = [c["name"] for c in inspector.get_columns("subscribers")]
    if "billing_mode" in existing_columns:
        op.drop_column("subscribers", "billing_mode")
