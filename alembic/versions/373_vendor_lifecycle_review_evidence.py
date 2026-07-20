"""Add staff review evidence to the vendor project lifecycle ledger.

Revision ID: 373_vendor_lifecycle_review
Revises: 372_vendor_payment_projection
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "373_vendor_lifecycle_review"
down_revision = "372_vendor_payment_projection"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "installation_project_lifecycle_events",
        sa.Column("reason", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("installation_project_lifecycle_events", "reason")
