"""Add reviewed consolidated settlement reconciliation provenance.

Revision ID: 318_consolidated_settlement_reconciliation
Revises: 317_consolidated_payment_returns
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "318_consolidated_settlement_reconciliation"
down_revision = "317_consolidated_payment_returns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "consolidated_payment_settlement_reconciliation_evidence",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True
        ),
        sa.Column("settlement_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider_event_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("payment_proof_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("topup_intent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(CASE WHEN provider_event_id IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN payment_proof_id IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN topup_intent_id IS NOT NULL THEN 1 ELSE 0 END) = 1",
            name="ck_consolidated_settle_recon_exactly_one_provenance",
        ),
        sa.ForeignKeyConstraint(
            ["settlement_id"], ["payment_settlements.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["provider_event_id"],
            ["payment_provider_events.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["payment_proof_id"], ["payment_proofs.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["topup_intent_id"], ["topup_intents.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "settlement_id", name="uq_consolidated_settle_recon_settlement"
        ),
        sa.UniqueConstraint(
            "provider_event_id", name="uq_consolidated_settle_recon_provider_event"
        ),
        sa.UniqueConstraint(
            "payment_proof_id", name="uq_consolidated_settle_recon_payment_proof"
        ),
        sa.UniqueConstraint(
            "topup_intent_id", name="uq_consolidated_settle_recon_topup_intent"
        ),
    )


def downgrade() -> None:
    op.drop_table("consolidated_payment_settlement_reconciliation_evidence")
