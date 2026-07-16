"""Add reviewed consolidated-credit consumption reconciliation evidence.

Revision ID: 320_consolidated_credit_consumption_reconciliation
Revises: 319_device_projection_table
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "320_consolidated_credit_consumption_reconciliation"
down_revision = "319_device_projection_table"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "consolidated_credit_consumption_reconciliation_evidence",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True
        ),
        sa.Column("allocation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("debit_action", sa.String(24), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "debit_action IN ('linked_existing', 'created_missing')",
            name="ck_consolidated_credit_recon_debit_action",
        ),
        sa.ForeignKeyConstraint(
            ["allocation_id"],
            ["billing_account_credit_allocations.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "allocation_id", name="uq_consolidated_credit_recon_allocation"
        ),
    )


def downgrade() -> None:
    op.drop_table("consolidated_credit_consumption_reconciliation_evidence")
