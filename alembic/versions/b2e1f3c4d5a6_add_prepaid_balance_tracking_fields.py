"""add_prepaid_balance_tracking_fields

Revision ID: b2e1f3c4d5a6
Revises: a4c5d8e7f901
Create Date: 2026-01-29 00:00:01.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "b2e1f3c4d5a6"
down_revision = "a4c5d8e7f901"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subscriber_accounts",
        sa.Column("prepaid_low_balance_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "subscriber_accounts",
        sa.Column("prepaid_deactivation_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("subscriber_accounts", "prepaid_deactivation_at")
    op.drop_column("subscriber_accounts", "prepaid_low_balance_at")
