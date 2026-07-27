"""Append-only evidence for the Sale -> Money shadow phase.

Warning logs are alerts, not cutover evidence: they rotate, and a consecutive
clean observation window cannot be proven from them. Each scan persists its
contract version, cohort fingerprint and full bucket counts here instead.

Revision ID: 426_sales_billing_shadow_runs
Revises: 425_sales_order_line_discounts
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "426_sales_billing_shadow_runs"
down_revision = "425_sales_order_line_discounts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sales_billing_shadow_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("contract_version", sa.Integer(), nullable=False),
        sa.Column("cohort_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("scanned", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bucket_counts", sa.JSON(), nullable=False),
        sa.Column(
            "clean", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("actor_id", sa.String(length=120), nullable=True),
        sa.CheckConstraint("scanned >= 0", name="ck_sales_billing_shadow_scanned"),
        sa.CheckConstraint(
            "contract_version > 0", name="ck_sales_billing_shadow_contract_version"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_sales_billing_shadow_runs_observed_at",
        "sales_billing_shadow_runs",
        ["observed_at"],
    )
    op.create_index(
        "ix_sales_billing_shadow_runs_version_observed",
        "sales_billing_shadow_runs",
        ["contract_version", "observed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_sales_billing_shadow_runs_version_observed",
        table_name="sales_billing_shadow_runs",
    )
    op.drop_index(
        "ix_sales_billing_shadow_runs_observed_at",
        table_name="sales_billing_shadow_runs",
    )
    op.drop_table("sales_billing_shadow_runs")
