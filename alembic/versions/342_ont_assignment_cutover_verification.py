"""Add immutable ONT cleanup verification attestations.

Revision ID: 342_ont_assignment_cutover_verification
Revises: 341_ont_assignment_cutover_batches
Create Date: 2026-07-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "342_ont_assignment_cutover_verification"
down_revision: str | None = "341_ont_assignment_cutover_batches"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ont_assignment_cutover_verification_attestations",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True
        ),
        sa.Column("proposal_batch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("batch_review_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("batch_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("decision_evidence_sha256", sa.String(64), nullable=False),
        sa.Column("fresh_report_sha256", sa.String(64), nullable=False),
        sa.Column("evidence_payload", sa.JSON(), nullable=False),
        sa.Column("evidence_sha256", sa.String(64), nullable=False),
        sa.Column("outcome", sa.String(48), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("pending_count", sa.Integer(), nullable=False),
        sa.Column("applied_count", sa.Integer(), nullable=False),
        sa.Column("declined_count", sa.Integer(), nullable=False),
        sa.Column("stale_closed_count", sa.Integer(), nullable=False),
        sa.Column("conflict_closed_count", sa.Integer(), nullable=False),
        sa.Column("other_closed_count", sa.Integer(), nullable=False),
        sa.Column("batch_scope_residual_count", sa.Integer(), nullable=False),
        sa.Column("global_blocker_assignment_count", sa.Integer(), nullable=False),
        sa.Column("global_cutover_ready", sa.Boolean(), nullable=False),
        sa.Column("verified_by", sa.String(160), nullable=False),
        sa.Column("verification_notes", sa.Text(), nullable=False),
        sa.Column("attestation_sha256", sa.String(64), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "outcome IN ('declined', 'applied_clean_scope', "
            "'applied_with_residual_findings', "
            "'completed_with_stale_closures', "
            "'completed_with_conflict_closures', "
            "'completed_with_other_closures')",
            name="ck_ont_assignment_cutover_verification_outcome",
        ),
        sa.CheckConstraint(
            "item_count BETWEEN 1 AND 100",
            name="ck_ont_assignment_cutover_verification_item_count",
        ),
        sa.CheckConstraint(
            "pending_count >= 0 AND applied_count >= 0 "
            "AND declined_count >= 0 AND stale_closed_count >= 0 "
            "AND conflict_closed_count >= 0 AND other_closed_count >= 0 "
            "AND batch_scope_residual_count >= 0 "
            "AND global_blocker_assignment_count >= 0",
            name="ck_ont_assignment_cutover_verification_counts_nonnegative",
        ),
        sa.CheckConstraint(
            "item_count = pending_count + applied_count + declined_count "
            "+ stale_closed_count + conflict_closed_count + other_closed_count",
            name="ck_ont_assignment_cutover_verification_counts_total",
        ),
        sa.CheckConstraint(
            "pending_count = 0",
            name="ck_ont_assignment_cutover_verification_terminal",
        ),
        sa.CheckConstraint(
            "length(batch_manifest_sha256) = 64 "
            "AND length(decision_evidence_sha256) = 64 "
            "AND length(fresh_report_sha256) = 64 "
            "AND length(evidence_sha256) = 64 "
            "AND length(attestation_sha256) = 64",
            name="ck_ont_assignment_cutover_verification_hashes",
        ),
        sa.ForeignKeyConstraint(
            ["proposal_batch_id"],
            ["ont_assignment_cutover_proposal_batches.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["batch_review_id"],
            ["ont_assignment_cutover_batch_reviews.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "proposal_batch_id",
            "evidence_sha256",
            name="uq_ont_assignment_cutover_verification_evidence",
        ),
        sa.UniqueConstraint(
            "attestation_sha256",
            name="uq_ont_assignment_cutover_verification_attestation",
        ),
    )
    op.create_index(
        "ix_ont_assignment_cutover_verification_batch",
        "ont_assignment_cutover_verification_attestations",
        ["proposal_batch_id", "verified_at"],
    )
    op.create_index(
        "ix_ont_assignment_cutover_verification_outcome",
        "ont_assignment_cutover_verification_attestations",
        ["outcome", "verified_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ont_assignment_cutover_verification_outcome",
        table_name="ont_assignment_cutover_verification_attestations",
    )
    op.drop_index(
        "ix_ont_assignment_cutover_verification_batch",
        table_name="ont_assignment_cutover_verification_attestations",
    )
    op.drop_table("ont_assignment_cutover_verification_attestations")
