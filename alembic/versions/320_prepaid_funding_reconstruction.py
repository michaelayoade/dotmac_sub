"""Add reviewed prepaid funding reconstruction authority.

Revision ID: 320_prepaid_funding_reconstruction
Revises: 319_prepaid_enforcement_readiness
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "320_prepaid_funding_reconstruction"
down_revision = "319_prepaid_enforcement_readiness"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "prepaid_funding_reconstruction_batches",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=240), nullable=False),
        sa.Column("evidence_ref", sa.Text(), nullable=False),
        sa.Column("position_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("account_count", sa.Integer(), nullable=False),
        sa.Column("total_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("approved_by", sa.String(length=120), nullable=False),
        sa.Column("is_authority_cutover", sa.Boolean(), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency)",
            name="ck_prepaid_funding_batch_currency",
        ),
        sa.CheckConstraint(
            "length(manifest_sha256) = 64",
            name="ck_prepaid_funding_batch_manifest_hash",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "manifest_sha256", name="uq_prepaid_funding_batch_manifest_sha256"
        ),
    )
    op.create_index(
        "uq_prepaid_funding_authority_cutover",
        "prepaid_funding_reconstruction_batches",
        ["is_authority_cutover"],
        unique=True,
        postgresql_where=sa.text("is_authority_cutover = true"),
    )
    op.create_table(
        "prepaid_funding_baselines",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("position_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency)",
            name="ck_prepaid_funding_baseline_currency",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["subscribers.id"],
            name="fk_prepaid_funding_baseline_account_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["prepaid_funding_reconstruction_batches.id"],
            name="fk_prepaid_funding_baseline_batch_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "batch_id",
            "account_id",
            "currency",
            name="uq_prepaid_funding_baseline_batch_account_currency",
        ),
    )
    op.create_index(
        "uq_prepaid_funding_baseline_active_account_currency",
        "prepaid_funding_baselines",
        ["account_id", "currency"],
        unique=True,
        postgresql_where=sa.text("is_active = true"),
    )
    op.create_index(
        "ix_prepaid_funding_baseline_batch_id",
        "prepaid_funding_baselines",
        ["batch_id"],
    )


def downgrade() -> None:
    cutover_count = op.get_bind().scalar(
        sa.text(
            "SELECT count(*) FROM prepaid_funding_reconstruction_batches "
            "WHERE is_authority_cutover = true"
        )
    )
    if int(cutover_count or 0) > 0:
        raise RuntimeError(
            "prepaid funding authority cutover is final; refusing to drop its SOT"
        )
    op.drop_index(
        "ix_prepaid_funding_baseline_batch_id",
        table_name="prepaid_funding_baselines",
    )
    op.drop_index(
        "uq_prepaid_funding_baseline_active_account_currency",
        table_name="prepaid_funding_baselines",
    )
    op.drop_table("prepaid_funding_baselines")
    op.drop_index(
        "uq_prepaid_funding_authority_cutover",
        table_name="prepaid_funding_reconstruction_batches",
    )
    op.drop_table("prepaid_funding_reconstruction_batches")
