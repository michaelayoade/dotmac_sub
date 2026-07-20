"""Add reviewed provenance for reconstructed historical return documents.

Revision ID: 327_consolidated_return_document_reconstruction
Revises: 326_consolidated_return_reconciliation
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "327_consolidated_return_document_reconstruction"
down_revision = "326_consolidated_return_reconciliation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "consolidated_payment_return_document_reconstruction_evidence",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True
        ),
        sa.Column(
            "reconciliation_evidence_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("historical_payment_state", sa.String(32), nullable=False),
        sa.Column("source_reference", sa.String(255), nullable=False),
        sa.Column("preview_fingerprint", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["reconciliation_evidence_id"],
            ["consolidated_payment_return_reconciliation_evidence.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "reconciliation_evidence_id",
            name="uq_consolidated_return_document_recon_evidence",
        ),
    )


def downgrade() -> None:
    op.drop_table("consolidated_payment_return_document_reconstruction_evidence")
