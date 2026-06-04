"""Add plan_family to catalog offers for self-service migration rules.

Revision ID: 113_add_catalog_offer_plan_family
Revises: 112_add_notifications_is_active_status_index
Create Date: 2026-05-26
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "113_add_catalog_offer_plan_family"
down_revision = "112_add_notifications_is_active_status_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "catalog_offers",
        sa.Column("plan_family", sa.String(length=40), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("catalog_offers", "plan_family")
