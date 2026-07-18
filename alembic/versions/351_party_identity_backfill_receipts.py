"""Add durable receipts for approved Subscriber identity backfills.

The table stores only digests, counts, approval-window timestamps, and the
PII-free plan manifest. It does not authorize or perform a backfill, merge,
repoint, role assignment, or lifecycle change.

Revision ID: 351_party_identity_backfill_receipts
Revises: 350_subscriber_party_binding
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "351_party_identity_backfill_receipts"
down_revision = "350_subscriber_party_binding"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "party_identity_backfill_receipts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("plan_digest", sa.String(length=64), nullable=False),
        sa.Column("audit_digest", sa.String(length=64), nullable=False),
        sa.Column("decision_file_sha256", sa.String(length=64), nullable=False),
        sa.Column("plan_file_sha256", sa.String(length=64), nullable=False),
        sa.Column("approval_file_sha256", sa.String(length=64), nullable=False),
        sa.Column("approved_by_sha256", sa.String(length=64), nullable=False),
        sa.Column("approval_reason_sha256", sa.String(length=64), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "applied_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("planned_party_count", sa.Integer(), nullable=False),
        sa.Column("binding_count", sa.Integer(), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.CheckConstraint(
            "length(plan_digest) = 64 AND length(audit_digest) = 64 AND "
            "length(decision_file_sha256) = 64 AND "
            "length(plan_file_sha256) = 64 AND "
            "length(approval_file_sha256) = 64 AND "
            "length(approved_by_sha256) = 64 AND "
            "length(approval_reason_sha256) = 64",
            name="ck_party_backfill_receipts_digest_lengths",
        ),
        sa.CheckConstraint(
            "planned_party_count >= 0 AND binding_count >= 0",
            name="ck_party_backfill_receipts_nonnegative_counts",
        ),
        sa.CheckConstraint(
            "expires_at >= approved_at",
            name="ck_party_backfill_receipts_approval_window",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_party_identity_backfill_receipts"),
        sa.UniqueConstraint(
            "plan_digest",
            name="uq_party_backfill_receipts_plan_digest",
        ),
    )
    op.create_index(
        "ix_party_backfill_receipts_applied_at",
        "party_identity_backfill_receipts",
        ["applied_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_party_backfill_receipts_applied_at",
        table_name="party_identity_backfill_receipts",
    )
    op.drop_table("party_identity_backfill_receipts")
