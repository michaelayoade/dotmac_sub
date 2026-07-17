"""Add immutable fiber-topology source staging manifests.

Revision ID: 333_fiber_topology_staging
Revises: 332_address_lga
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "333_fiber_topology_staging"
down_revision = "332_address_lga"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fiber_topology_source_batches",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True
        ),
        sa.Column("source_system", sa.String(40), nullable=False),
        sa.Column("profile", sa.String(40), nullable=False),
        sa.Column("source_name", sa.String(255), nullable=False),
        sa.Column("asset_type", sa.String(40), nullable=False),
        sa.Column("external_id_key", sa.String(80), nullable=False),
        sa.Column("file_sha256", sa.String(64), nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("feature_count", sa.Integer(), nullable=False),
        sa.Column("blocker_count", sa.Integer(), nullable=False),
        sa.Column("candidate_count", sa.Integer(), nullable=False),
        sa.Column("unchanged_count", sa.Integer(), nullable=False),
        sa.Column("new_count", sa.Integer(), nullable=False),
        sa.Column("source_metadata", sa.JSON(), nullable=True),
        sa.Column("created_by", sa.String(160), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('staged', 'blocked')",
            name="ck_fiber_topology_batch_status",
        ),
        sa.CheckConstraint(
            "(status = 'blocked' AND blocker_count > 0) OR "
            "(status = 'staged' AND blocker_count = 0)",
            name="ck_fiber_topology_batch_status_blockers",
        ),
        sa.CheckConstraint(
            "feature_count >= 0 AND blocker_count >= 0 AND candidate_count >= 0 "
            "AND unchanged_count >= 0 AND new_count >= 0 AND "
            "blocker_count + candidate_count + unchanged_count + new_count "
            "= feature_count",
            name="ck_fiber_topology_batch_status_counts",
        ),
        sa.CheckConstraint(
            "length(file_sha256) = 64",
            name="ck_fiber_topology_batch_file_sha256",
        ),
        sa.CheckConstraint(
            "length(manifest_sha256) = 64",
            name="ck_fiber_topology_batch_manifest_sha256",
        ),
        sa.UniqueConstraint(
            "source_system",
            "profile",
            "manifest_sha256",
            name="uq_fiber_topology_batch_source_profile_manifest",
        ),
    )
    op.create_index(
        "ix_fiber_topology_batch_profile_created",
        "fiber_topology_source_batches",
        ["profile", "created_at"],
    )
    op.create_index(
        "ix_fiber_topology_batch_file_sha256",
        "fiber_topology_source_batches",
        ["file_sha256"],
    )

    op.create_table(
        "fiber_topology_staged_features",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True
        ),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("asset_type", sa.String(40), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=True),
        sa.Column("display_name", sa.String(255), nullable=True),
        sa.Column("geometry_type", sa.String(20), nullable=False),
        sa.Column("geometry_geojson", sa.JSON(), nullable=False),
        sa.Column("source_properties", sa.JSON(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("geometry_sha256", sa.String(64), nullable=False),
        sa.Column("match_status", sa.String(20), nullable=False),
        sa.Column("blocker_codes", sa.JSON(), nullable=False),
        sa.Column("match_reasons", sa.JSON(), nullable=False),
        sa.Column("candidate_asset_ids", sa.JSON(), nullable=False),
        sa.Column("canonical_asset_type", sa.String(40), nullable=True),
        sa.Column("canonical_asset_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("prior_feature_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "match_status IN "
            "('new', 'unchanged', 'exact_external', 'candidate', "
            "'ambiguous', 'blocked')",
            name="ck_fiber_topology_staged_feature_match_status",
        ),
        sa.CheckConstraint(
            "external_id IS NOT NULL OR match_status = 'blocked'",
            name="ck_fiber_topology_staged_feature_identity",
        ),
        sa.CheckConstraint(
            "length(content_sha256) = 64",
            name="ck_fiber_topology_staged_feature_content_sha256",
        ),
        sa.CheckConstraint(
            "length(geometry_sha256) = 64",
            name="ck_fiber_topology_staged_feature_geometry_sha256",
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["fiber_topology_source_batches.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["prior_feature_id"],
            ["fiber_topology_staged_features.id"],
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "batch_id",
            "row_number",
            name="uq_fiber_topology_staged_feature_batch_row",
        ),
    )
    op.create_index(
        "ix_fiber_topology_staged_feature_identity",
        "fiber_topology_staged_features",
        ["asset_type", "external_id"],
    )
    op.create_index(
        "ix_fiber_topology_staged_feature_content_sha256",
        "fiber_topology_staged_features",
        ["content_sha256"],
    )
    op.create_index(
        "ix_fiber_topology_staged_feature_geometry_sha256",
        "fiber_topology_staged_features",
        ["geometry_sha256"],
    )
    op.create_index(
        "ix_fiber_topology_staged_feature_match_status",
        "fiber_topology_staged_features",
        ["match_status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_fiber_topology_staged_feature_match_status",
        table_name="fiber_topology_staged_features",
    )
    op.drop_index(
        "ix_fiber_topology_staged_feature_geometry_sha256",
        table_name="fiber_topology_staged_features",
    )
    op.drop_index(
        "ix_fiber_topology_staged_feature_content_sha256",
        table_name="fiber_topology_staged_features",
    )
    op.drop_index(
        "ix_fiber_topology_staged_feature_identity",
        table_name="fiber_topology_staged_features",
    )
    op.drop_table("fiber_topology_staged_features")
    op.drop_index(
        "ix_fiber_topology_batch_file_sha256",
        table_name="fiber_topology_source_batches",
    )
    op.drop_index(
        "ix_fiber_topology_batch_profile_created",
        table_name="fiber_topology_source_batches",
    )
    op.drop_table("fiber_topology_source_batches")
