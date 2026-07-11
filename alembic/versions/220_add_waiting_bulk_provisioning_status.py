"""Add waiting status to bulk provisioning items.

Revision ID: 220_add_waiting_bulk_provisioning_status
Revises: 219_add_drift_finding_evidence
Create Date: 2026-07-11
"""

from alembic import op

revision = "220_add_waiting_bulk_provisioning_status"
down_revision = "219_add_drift_finding_evidence"
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
