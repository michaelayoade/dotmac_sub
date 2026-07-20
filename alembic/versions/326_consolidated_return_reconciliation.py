"""Add reviewed consolidated refund/reversal reconciliation evidence.

Revision ID: 326_consolidated_return_reconciliation
Revises: 325_consolidated_credit_consumption_reconciliation
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "326_consolidated_return_reconciliation"
down_revision = "325_consolidated_credit_consumption_reconciliation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "consolidated_payment_return_reconciliation_evidence",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True
        ),
        sa.Column("refund_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reversal_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("preview_fingerprint", sa.String(64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(CASE WHEN refund_id IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN reversal_id IS NOT NULL THEN 1 ELSE 0 END) = 1",
            name="ck_consolidated_return_recon_exactly_one_owner",
        ),
        sa.ForeignKeyConstraint(
            ["refund_id"], ["payment_refunds.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["reversal_id"], ["payment_reversals.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("refund_id", name="uq_consolidated_return_recon_refund"),
        sa.UniqueConstraint(
            "reversal_id", name="uq_consolidated_return_recon_reversal"
        ),
    )


def downgrade() -> None:
    op.drop_table("consolidated_payment_return_reconciliation_evidence")
