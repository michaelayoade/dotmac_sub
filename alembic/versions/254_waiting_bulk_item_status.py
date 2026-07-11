"""Add waiting status to bulk provisioning items.

Revision ID: 254_waiting_bulk_item_status
Revises: 253_billing_updated_since_indexes
Create Date: 2026-07-11
"""

from alembic import op

revision = "254_waiting_bulk_item_status"
down_revision = "253_billing_updated_since_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TYPE bulkprovisioningitemstatus "
        "ADD VALUE IF NOT EXISTS 'waiting' AFTER 'running'"
    )


def downgrade() -> None:
    # PostgreSQL enum values cannot be removed without rebuilding the type.
    pass
