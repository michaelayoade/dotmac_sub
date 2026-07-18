"""Add immutable reviewed ONT assignment cutover batches.

Revision ID: 341_ont_assignment_cutover_batches
Revises: 340_ont_topology_observation_evidence
Create Date: 2026-07-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "341_ont_assignment_cutover_batches"
down_revision: str | None = "340_ont_topology_observation_evidence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ont_assignment_cutover_proposal_batches",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True
        ),
        sa.Column("report_sha256", sa.String(64), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("manifest_payload", sa.JSON(), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("source_name", sa.String(255), nullable=False),
        sa.Column("proposed_by", sa.String(160), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(report_sha256) = 64",
            name="ck_ont_assignment_cutover_batch_report_sha256",
        ),
        sa.CheckConstraint(
            "length(request_sha256) = 64",
            name="ck_ont_assignment_cutover_batch_request_sha256",
        ),
        sa.CheckConstraint(
            "length(manifest_sha256) = 64",
            name="ck_ont_assignment_cutover_batch_manifest_sha256",
        ),
        sa.CheckConstraint(
            "item_count BETWEEN 1 AND 100",
            name="ck_ont_assignment_cutover_batch_item_count",
        ),
        sa.UniqueConstraint(
            "request_sha256", name="uq_ont_assignment_cutover_batch_request"
        ),
        sa.UniqueConstraint(
            "manifest_sha256", name="uq_ont_assignment_cutover_batch_manifest"
        ),
    )
    op.create_index(
        "ix_ont_assignment_cutover_batch_created",
        "ont_assignment_cutover_proposal_batches",
        ["created_at"],
    )
    op.create_table(
        "ont_assignment_cutover_batch_reviews",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True
        ),
        sa.Column("proposal_batch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("batch_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("proposed_by", sa.String(160), nullable=False),
        sa.Column("reviewed_by", sa.String(160), nullable=False),
        sa.Column("review_notes", sa.Text(), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("attestation_sha256", sa.String(64), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "action IN ('approve', 'decline')",
            name="ck_ont_assignment_cutover_batch_review_action",
        ),
        sa.CheckConstraint(
            "proposed_by <> reviewed_by",
            name="ck_ont_assignment_cutover_batch_review_separation",
        ),
        sa.CheckConstraint(
            "length(batch_manifest_sha256) = 64",
            name="ck_ont_assignment_cutover_batch_review_manifest_sha256",
        ),
        sa.CheckConstraint(
            "length(attestation_sha256) = 64",
            name="ck_ont_assignment_cutover_batch_review_sha256",
        ),
        sa.CheckConstraint(
            "item_count BETWEEN 1 AND 100",
            name="ck_ont_assignment_cutover_batch_review_item_count",
        ),
        sa.ForeignKeyConstraint(
            ["proposal_batch_id"],
            ["ont_assignment_cutover_proposal_batches.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "proposal_batch_id",
            name="uq_ont_assignment_cutover_batch_review_batch",
        ),
        sa.UniqueConstraint(
            "attestation_sha256",
            name="uq_ont_assignment_cutover_batch_review_attestation",
        ),
    )
    op.create_index(
        "ix_ont_assignment_cutover_batch_reviewed",
        "ont_assignment_cutover_batch_reviews",
        ["reviewed_at"],
    )
    op.add_column(
        "ont_assignment_identity_decisions",
        sa.Column("proposal_batch_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "ont_assignment_identity_decisions",
        sa.Column("proposal_batch_row_number", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_ont_assignment_identity_proposal_batch",
        "ont_assignment_identity_decisions",
        "ont_assignment_cutover_proposal_batches",
        ["proposal_batch_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_ont_assignment_identity_batch_evidence",
        "ont_assignment_identity_decisions",
        "(proposal_batch_id IS NULL AND proposal_batch_row_number IS NULL) OR "
        "(proposal_batch_id IS NOT NULL AND proposal_batch_row_number > 0)",
    )
    op.create_unique_constraint(
        "uq_ont_assignment_identity_batch_row",
        "ont_assignment_identity_decisions",
        ["proposal_batch_id", "proposal_batch_row_number"],
    )
    op.create_index(
        "ix_ont_assignment_identity_batch",
        "ont_assignment_identity_decisions",
        ["proposal_batch_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ont_assignment_identity_batch",
        table_name="ont_assignment_identity_decisions",
    )
    op.drop_constraint(
        "uq_ont_assignment_identity_batch_row",
        "ont_assignment_identity_decisions",
        type_="unique",
    )
    op.drop_constraint(
        "ck_ont_assignment_identity_batch_evidence",
        "ont_assignment_identity_decisions",
        type_="check",
    )
    op.drop_constraint(
        "fk_ont_assignment_identity_proposal_batch",
        "ont_assignment_identity_decisions",
        type_="foreignkey",
    )
    op.drop_column("ont_assignment_identity_decisions", "proposal_batch_row_number")
    op.drop_column("ont_assignment_identity_decisions", "proposal_batch_id")
    op.drop_index(
        "ix_ont_assignment_cutover_batch_reviewed",
        table_name="ont_assignment_cutover_batch_reviews",
    )
    op.drop_table("ont_assignment_cutover_batch_reviews")
    op.drop_index(
        "ix_ont_assignment_cutover_batch_created",
        table_name="ont_assignment_cutover_proposal_batches",
    )
    op.drop_table("ont_assignment_cutover_proposal_batches")
