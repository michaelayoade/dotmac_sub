"""Add append-only classification review for staged map features.

Revision ID: 642_network_map_import_feature_classification
Revises: 641_customer_vacation_pause_resume_at
Create Date: 2026-10-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "642_network_map_import_feature_classification"
down_revision: str | None = "641_customer_vacation_pause_resume_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "fiber_topology_feature_classification_reviews",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("staged_feature_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("asset_type", sa.String(length=40), nullable=False),
        sa.Column("command_key_sha256", sa.String(length=64), nullable=False),
        sa.Column("command_fingerprint_sha256", sa.String(length=64), nullable=False),
        sa.Column("reviewed_by", sa.String(length=160), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "asset_type IN ('fiber_segment', 'fiber_access_point', 'fdh_cabinet', "
            "'splice_closure', 'service_building', 'support_structure')",
            name="ck_fiber_topology_feature_classification_asset_type",
        ),
        sa.CheckConstraint(
            "revision > 0", name="ck_fiber_topology_feature_classification_revision"
        ),
        sa.CheckConstraint(
            "length(command_key_sha256) = 64",
            name="ck_fiber_topology_feature_classification_command_key_sha256",
        ),
        sa.CheckConstraint(
            "length(command_fingerprint_sha256) = 64",
            name="ck_fiber_topology_classification_fingerprint_sha256",
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["fiber_topology_source_batches.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["staged_feature_id"],
            ["fiber_topology_staged_features.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "staged_feature_id",
            "revision",
            name="uq_fiber_topology_feature_classification_revision",
        ),
        sa.UniqueConstraint(
            "batch_id",
            "command_key_sha256",
            "staged_feature_id",
            name="uq_fiber_topology_feature_classification_command_row",
        ),
    )
    op.create_index(
        "ix_fiber_topology_feature_classification_feature_revision",
        "fiber_topology_feature_classification_reviews",
        ["staged_feature_id", "revision"],
        unique=False,
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION fiber_topology_feature_classification_append_only()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'fiber topology feature classifications are append-only';
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER fiber_topology_feature_classification_reviews_append_only
        BEFORE UPDATE OR DELETE ON fiber_topology_feature_classification_reviews
        FOR EACH ROW EXECUTE FUNCTION fiber_topology_feature_classification_append_only();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER IF EXISTS fiber_topology_feature_classification_reviews_append_only
        ON fiber_topology_feature_classification_reviews;
        DROP FUNCTION IF EXISTS fiber_topology_feature_classification_append_only();
        """
    )
    op.drop_index(
        "ix_fiber_topology_feature_classification_feature_revision",
        table_name="fiber_topology_feature_classification_reviews",
    )
    op.drop_table("fiber_topology_feature_classification_reviews")
