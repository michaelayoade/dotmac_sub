"""Add a durable scheduled-resume instant to subscription pause causes.

Revision ID: 641_customer_vacation_pause_resume_at
Revises: 640_catalog_price_tax_application
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "641_customer_vacation_pause_resume_at"
down_revision: str | None = "640_catalog_price_tax_application"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "subscription_pause_causes",
        sa.Column("scheduled_resume_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_subscription_pause_causes_scheduled_resume_at",
        "subscription_pause_causes",
        ["scheduled_resume_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_subscription_pause_causes_scheduled_resume_at",
        table_name="subscription_pause_causes",
    )
    op.drop_column("subscription_pause_causes", "scheduled_resume_at")
