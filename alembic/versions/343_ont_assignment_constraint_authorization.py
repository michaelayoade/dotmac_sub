"""Add immutable ONT assignment constraint authorization evidence.

Revision ID: 343_ont_assignment_constraint_authorization
Revises: 342_ont_assignment_cutover_verification
Create Date: 2026-07-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "343_ont_assignment_constraint_authorization"
down_revision: str | None = "342_ont_assignment_cutover_verification"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ont_assignment_constraint_authorization_requests",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True
        ),
        sa.Column("target_environment", sa.String(255), nullable=False),
        sa.Column("coverage_report_sha256", sa.String(64), nullable=False),
        sa.Column("cutover_report_sha256", sa.String(64), nullable=False),
        sa.Column("coverage_payload", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requested_by", sa.String(160), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(coverage_report_sha256) = 64 "
            "AND length(cutover_report_sha256) = 64 "
            "AND length(request_sha256) = 64",
            name="ck_ont_assignment_constraint_authorization_request_hashes",
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name="ck_ont_assignment_constraint_authorization_request_expiry",
        ),
        sa.UniqueConstraint(
            "request_sha256",
            name="uq_ont_assignment_constraint_authorization_request_sha256",
        ),
    )
    op.create_index(
        "ix_ont_assignment_constraint_authorization_request_target",
        "ont_assignment_constraint_authorization_requests",
        ["target_environment", "created_at"],
    )
    op.create_table(
        "ont_assignment_constraint_authorization_reviews",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True
        ),
        sa.Column(
            "authorization_request_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("current_coverage_report_sha256", sa.String(64), nullable=False),
        sa.Column("current_cutover_report_sha256", sa.String(64), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("requested_by", sa.String(160), nullable=False),
        sa.Column("reviewed_by", sa.String(160), nullable=False),
        sa.Column("review_notes", sa.Text(), nullable=False),
        sa.Column("attestation_sha256", sa.String(64), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "action IN ('approve', 'decline')",
            name="ck_ont_assignment_constraint_authorization_review_action",
        ),
        sa.CheckConstraint(
            "requested_by <> reviewed_by",
            name="ck_ont_assignment_constraint_authorization_review_separation",
        ),
        sa.CheckConstraint(
            "length(request_sha256) = 64 "
            "AND length(current_coverage_report_sha256) = 64 "
            "AND length(current_cutover_report_sha256) = 64 "
            "AND length(attestation_sha256) = 64",
            name="ck_ont_assignment_constraint_authorization_review_hashes",
        ),
        sa.ForeignKeyConstraint(
            ["authorization_request_id"],
            ["ont_assignment_constraint_authorization_requests.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "authorization_request_id",
            name="uq_ont_assignment_constraint_authorization_review_request",
        ),
        sa.UniqueConstraint(
            "attestation_sha256",
            name="uq_ont_assignment_constraint_authorization_review_attestation",
        ),
    )
    op.create_index(
        "ix_ont_assignment_constraint_authorization_reviewed",
        "ont_assignment_constraint_authorization_reviews",
        ["reviewed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ont_assignment_constraint_authorization_reviewed",
        table_name="ont_assignment_constraint_authorization_reviews",
    )
    op.drop_table("ont_assignment_constraint_authorization_reviews")
    op.drop_index(
        "ix_ont_assignment_constraint_authorization_request_target",
        table_name="ont_assignment_constraint_authorization_requests",
    )
    op.drop_table("ont_assignment_constraint_authorization_requests")
