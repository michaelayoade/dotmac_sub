"""Add restrict_to_assigned_offers to resellers (C-2 catalog visibility).

Revision ID: 204_add_reseller_restrict_to_assigned_offers
Revises: 203_add_notification_template_conditions
Create Date: 2026-07-03
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "204_add_reseller_restrict_to_assigned_offers"
down_revision = "203_add_notification_template_conditions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "resellers",
        sa.Column("restrict_to_assigned_offers", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("resellers", "restrict_to_assigned_offers")
