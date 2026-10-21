"""Restricted field identity manual review lifecycle.

Revision ID: 672_field_identity_review
Revises: 671_assignment_generation
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "672_field_identity_review"
down_revision = "671_assignment_generation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "field_identity_revisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "subject_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("system_users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("display_name", sa.String(120), nullable=False),
        sa.Column("company_name", sa.String(120), nullable=False),
        sa.Column(
            "portrait_file_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("stored_files.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "evidence_file_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("stored_files.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "reviewer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("system_users.id", ondelete="RESTRICT"),
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("review_reason", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "subject_id", "revision", name="uq_field_identity_revision"
        ),
        sa.CheckConstraint(
            "state IN ('pending','reviewed','rejected','revoked')",
            name="ck_field_identity_state",
        ),
        sa.CheckConstraint(
            "revision > 0 AND version > 0", name="ck_field_identity_versions"
        ),
        sa.CheckConstraint(
            "(state = 'pending' AND reviewer_id IS NULL) OR (state <> 'pending' AND reviewer_id IS NOT NULL AND reviewed_at IS NOT NULL AND review_reason IS NOT NULL AND reviewer_id <> subject_id AND length(trim(review_reason)) > 0)",
            name="ck_field_identity_review",
        ),
    )
    op.create_table(
        "field_identity_receipts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "subject_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("system_users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(120), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.UniqueConstraint(
            "subject_id", "idempotency_key", name="uq_field_identity_receipt"
        ),
    )


def downgrade() -> None:
    op.drop_table("field_identity_receipts")
    op.drop_table("field_identity_revisions")
