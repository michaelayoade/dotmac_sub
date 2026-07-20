"""Add reviewed fiber connectivity batch control and run evidence.

Revision ID: 344_fiber_connectivity_batch_control
Revises: 343_ont_assignment_constraint_authorization
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "344_fiber_connectivity_batch_control"
down_revision = "343_ont_assignment_constraint_authorization"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fiber_topology_connectivity_proposal_batches",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True
        ),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("manifest_payload", sa.JSON(), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("source_name", sa.String(255), nullable=False),
        sa.Column("proposed_by", sa.String(160), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(manifest_sha256) = 64 AND length(request_sha256) = 64",
            name="ck_fiber_connectivity_proposal_batch_hashes",
        ),
        sa.CheckConstraint(
            "item_count BETWEEN 1 AND 500",
            name="ck_fiber_connectivity_proposal_batch_item_count",
        ),
        sa.UniqueConstraint(
            "manifest_sha256", name="uq_fiber_connectivity_proposal_batch_manifest"
        ),
        sa.UniqueConstraint(
            "request_sha256", name="uq_fiber_connectivity_proposal_batch_request"
        ),
    )
    op.create_index(
        "ix_fiber_connectivity_proposal_batch_created",
        "fiber_topology_connectivity_proposal_batches",
        ["created_at"],
    )

    op.add_column(
        "fiber_topology_connectivity_decisions",
        sa.Column("proposal_batch_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "fiber_topology_connectivity_decisions",
        sa.Column("proposal_batch_row_number", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_fiber_connectivity_decision_proposal_batch",
        "fiber_topology_connectivity_decisions",
        "fiber_topology_connectivity_proposal_batches",
        ["proposal_batch_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_fiber_connectivity_decision_batch_evidence",
        "fiber_topology_connectivity_decisions",
        "(proposal_batch_id IS NULL AND proposal_batch_row_number IS NULL) OR "
        "(proposal_batch_id IS NOT NULL AND proposal_batch_row_number > 0)",
    )
    op.create_unique_constraint(
        "uq_fiber_connectivity_decision_batch_row",
        "fiber_topology_connectivity_decisions",
        ["proposal_batch_id", "proposal_batch_row_number"],
    )
    op.create_index(
        "ix_fiber_connectivity_decision_batch",
        "fiber_topology_connectivity_decisions",
        ["proposal_batch_id"],
    )

    op.create_table(
        "fiber_topology_connectivity_batch_reviews",
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
            name="ck_fiber_connectivity_batch_review_action",
        ),
        sa.CheckConstraint(
            "proposed_by <> reviewed_by",
            name="ck_fiber_connectivity_batch_review_separation",
        ),
        sa.CheckConstraint(
            "length(batch_manifest_sha256) = 64 AND length(attestation_sha256) = 64",
            name="ck_fiber_connectivity_batch_review_hashes",
        ),
        sa.CheckConstraint(
            "item_count BETWEEN 1 AND 500",
            name="ck_fiber_connectivity_batch_review_item_count",
        ),
        sa.ForeignKeyConstraint(
            ["proposal_batch_id"],
            ["fiber_topology_connectivity_proposal_batches.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "proposal_batch_id", name="uq_fiber_connectivity_batch_review_batch"
        ),
        sa.UniqueConstraint(
            "attestation_sha256",
            name="uq_fiber_connectivity_batch_review_attestation",
        ),
    )
    op.create_index(
        "ix_fiber_connectivity_batch_review_reviewed",
        "fiber_topology_connectivity_batch_reviews",
        ["reviewed_at"],
    )

    op.create_table(
        "fiber_topology_connectivity_runs",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True
        ),
        sa.Column("proposal_batch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("batch_review_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("batch_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("run_type", sa.String(16), nullable=False),
        sa.Column("executed_by", sa.String(160), nullable=False),
        sa.Column("requested_limit", sa.Integer(), nullable=False),
        sa.Column("scanned_count", sa.Integer(), nullable=False),
        sa.Column("endpoint_pending_count", sa.Integer(), nullable=False),
        sa.Column("segment_pending_count", sa.Integer(), nullable=False),
        sa.Column("applied_count", sa.Integer(), nullable=False),
        sa.Column("closed_count", sa.Integer(), nullable=False),
        sa.Column("error_count", sa.Integer(), nullable=False),
        sa.Column("remaining_actionable_count", sa.Integer(), nullable=False),
        sa.Column("result_payload", sa.JSON(), nullable=False),
        sa.Column("result_sha256", sa.String(64), nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "run_type IN ('execute', 'reconcile')",
            name="ck_fiber_connectivity_run_type",
        ),
        sa.CheckConstraint(
            "length(batch_manifest_sha256) = 64 AND length(result_sha256) = 64",
            name="ck_fiber_connectivity_run_hashes",
        ),
        sa.CheckConstraint(
            "requested_limit BETWEEN 1 AND 100",
            name="ck_fiber_connectivity_run_limit",
        ),
        sa.CheckConstraint(
            "scanned_count >= 0 AND endpoint_pending_count >= 0 "
            "AND segment_pending_count >= 0 AND applied_count >= 0 "
            "AND closed_count >= 0 AND error_count >= 0 "
            "AND remaining_actionable_count >= 0",
            name="ck_fiber_connectivity_run_counts",
        ),
        sa.CheckConstraint(
            "scanned_count = endpoint_pending_count + segment_pending_count "
            "+ applied_count + closed_count + error_count",
            name="ck_fiber_connectivity_run_outcomes",
        ),
        sa.ForeignKeyConstraint(
            ["proposal_batch_id"],
            ["fiber_topology_connectivity_proposal_batches.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["batch_review_id"],
            ["fiber_topology_connectivity_batch_reviews.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("result_sha256", name="uq_fiber_connectivity_run_result"),
    )
    op.create_index(
        "ix_fiber_connectivity_run_batch",
        "fiber_topology_connectivity_runs",
        ["proposal_batch_id"],
    )
    op.create_index(
        "ix_fiber_connectivity_run_executed",
        "fiber_topology_connectivity_runs",
        ["executed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_fiber_connectivity_run_executed",
        table_name="fiber_topology_connectivity_runs",
    )
    op.drop_index(
        "ix_fiber_connectivity_run_batch",
        table_name="fiber_topology_connectivity_runs",
    )
    op.drop_table("fiber_topology_connectivity_runs")
    op.drop_index(
        "ix_fiber_connectivity_batch_review_reviewed",
        table_name="fiber_topology_connectivity_batch_reviews",
    )
    op.drop_table("fiber_topology_connectivity_batch_reviews")
    op.drop_index(
        "ix_fiber_connectivity_decision_batch",
        table_name="fiber_topology_connectivity_decisions",
    )
    op.drop_constraint(
        "uq_fiber_connectivity_decision_batch_row",
        "fiber_topology_connectivity_decisions",
        type_="unique",
    )
    op.drop_constraint(
        "ck_fiber_connectivity_decision_batch_evidence",
        "fiber_topology_connectivity_decisions",
        type_="check",
    )
    op.drop_constraint(
        "fk_fiber_connectivity_decision_proposal_batch",
        "fiber_topology_connectivity_decisions",
        type_="foreignkey",
    )
    op.drop_column("fiber_topology_connectivity_decisions", "proposal_batch_row_number")
    op.drop_column("fiber_topology_connectivity_decisions", "proposal_batch_id")
    op.drop_index(
        "ix_fiber_connectivity_proposal_batch_created",
        table_name="fiber_topology_connectivity_proposal_batches",
    )
    op.drop_table("fiber_topology_connectivity_proposal_batches")
