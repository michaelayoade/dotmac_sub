"""Add fiber identity batch attestation and bounded execution evidence.

Revision ID: 336_fiber_identity_batch_control
Revises: 335_fiber_identity_review_batches
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "336_fiber_identity_batch_control"
down_revision = "335_fiber_identity_review_batches"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fiber_topology_identity_batch_reviews",
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
            name="ck_fiber_identity_batch_review_action",
        ),
        sa.CheckConstraint(
            "proposed_by <> reviewed_by",
            name="ck_fiber_identity_batch_review_separation",
        ),
        sa.CheckConstraint(
            "length(batch_manifest_sha256) = 64",
            name="ck_fiber_identity_batch_review_manifest_sha256",
        ),
        sa.CheckConstraint(
            "length(attestation_sha256) = 64",
            name="ck_fiber_identity_batch_review_sha256",
        ),
        sa.CheckConstraint(
            "item_count > 0",
            name="ck_fiber_identity_batch_review_item_count",
        ),
        sa.ForeignKeyConstraint(
            ["proposal_batch_id"],
            ["fiber_topology_identity_proposal_batches.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "proposal_batch_id", name="uq_fiber_identity_batch_review_batch"
        ),
        sa.UniqueConstraint(
            "attestation_sha256",
            name="uq_fiber_identity_batch_review_attestation",
        ),
    )
    op.create_index(
        "ix_fiber_identity_batch_review_reviewed",
        "fiber_topology_identity_batch_reviews",
        ["reviewed_at"],
    )

    op.create_table(
        "fiber_topology_identity_execution_runs",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True
        ),
        sa.Column("proposal_batch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("batch_review_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("batch_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("executed_by", sa.String(160), nullable=False),
        sa.Column("requested_limit", sa.Integer(), nullable=False),
        sa.Column("scanned_count", sa.Integer(), nullable=False),
        sa.Column("change_requested_count", sa.Integer(), nullable=False),
        sa.Column("applied_count", sa.Integer(), nullable=False),
        sa.Column("closed_count", sa.Integer(), nullable=False),
        sa.Column("error_count", sa.Integer(), nullable=False),
        sa.Column("remaining_approved_count", sa.Integer(), nullable=False),
        sa.Column("result_payload", sa.JSON(), nullable=False),
        sa.Column("result_sha256", sa.String(64), nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(batch_manifest_sha256) = 64",
            name="ck_fiber_identity_execution_manifest_sha256",
        ),
        sa.CheckConstraint(
            "length(result_sha256) = 64",
            name="ck_fiber_identity_execution_result_sha256",
        ),
        sa.CheckConstraint(
            "requested_limit BETWEEN 1 AND 100",
            name="ck_fiber_identity_execution_limit",
        ),
        sa.CheckConstraint(
            "scanned_count >= 0 AND change_requested_count >= 0 "
            "AND applied_count >= 0 AND closed_count >= 0 "
            "AND error_count >= 0 AND remaining_approved_count >= 0",
            name="ck_fiber_identity_execution_counts",
        ),
        sa.CheckConstraint(
            "scanned_count = change_requested_count + applied_count "
            "+ closed_count + error_count",
            name="ck_fiber_identity_execution_outcomes",
        ),
        sa.ForeignKeyConstraint(
            ["proposal_batch_id"],
            ["fiber_topology_identity_proposal_batches.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["batch_review_id"],
            ["fiber_topology_identity_batch_reviews.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "result_sha256", name="uq_fiber_identity_execution_run_result"
        ),
    )
    op.create_index(
        "ix_fiber_identity_execution_batch",
        "fiber_topology_identity_execution_runs",
        ["proposal_batch_id"],
    )
    op.create_index(
        "ix_fiber_identity_execution_executed",
        "fiber_topology_identity_execution_runs",
        ["executed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_fiber_identity_execution_executed",
        table_name="fiber_topology_identity_execution_runs",
    )
    op.drop_index(
        "ix_fiber_identity_execution_batch",
        table_name="fiber_topology_identity_execution_runs",
    )
    op.drop_table("fiber_topology_identity_execution_runs")
    op.drop_index(
        "ix_fiber_identity_batch_review_reviewed",
        table_name="fiber_topology_identity_batch_reviews",
    )
    op.drop_table("fiber_topology_identity_batch_reviews")
