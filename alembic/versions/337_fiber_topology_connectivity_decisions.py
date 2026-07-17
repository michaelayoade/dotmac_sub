"""Add reviewed fiber termination and segment connectivity decisions.

Revision ID: 337_fiber_topology_connectivity_decisions
Revises: 336_fiber_identity_batch_control
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "337_fiber_topology_connectivity_decisions"
down_revision = "336_fiber_identity_batch_control"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TYPE odnendpointtype ADD VALUE IF NOT EXISTS 'fiber_access_point'"
    )
    op.create_index(
        "uq_fiber_termination_active_endpoint",
        "fiber_termination_points",
        ["endpoint_type", "ref_id"],
        unique=True,
        postgresql_where=sa.text("is_active AND ref_id IS NOT NULL"),
    )

    op.create_table(
        "fiber_topology_connectivity_decisions",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True
        ),
        sa.Column("staged_feature_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_system", sa.String(40), nullable=False),
        sa.Column("source_asset_type", sa.String(40), nullable=False),
        sa.Column("source_external_id", sa.String(255), nullable=False),
        sa.Column("feature_content_sha256", sa.String(64), nullable=False),
        sa.Column("action", sa.String(24), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("start_endpoint_type", sa.String(40), nullable=True),
        sa.Column(
            "start_endpoint_ref_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("end_endpoint_type", sa.String(40), nullable=True),
        sa.Column("end_endpoint_ref_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("segment_type", sa.String(32), nullable=True),
        sa.Column("cable_type", sa.String(32), nullable=True),
        sa.Column("fiber_count", sa.Integer(), nullable=True),
        sa.Column("length_m", sa.Float(), nullable=True),
        sa.Column("target_segment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("start_resolution_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("end_resolution_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "segment_change_request_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("canonical_segment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("decision_sha256", sa.String(64), nullable=False),
        sa.Column("proposed_by", sa.String(160), nullable=False),
        sa.Column("proposed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_by", sa.String(160), nullable=True),
        sa.Column("review_notes", sa.Text(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("executed_by", sa.String(160), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finalized_by", sa.String(160), nullable=True),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_reason", sa.String(160), nullable=True),
        sa.CheckConstraint(
            "action IN ('create', 'link_existing', 'reject')",
            name="ck_fiber_connectivity_action",
        ),
        sa.CheckConstraint(
            "status IN ('proposed', 'approved', 'declined', "
            "'endpoint_change_requested', 'segment_change_requested', "
            "'applied', 'closed')",
            name="ck_fiber_connectivity_status",
        ),
        sa.CheckConstraint(
            "(action = 'reject' AND start_endpoint_type IS NULL "
            "AND start_endpoint_ref_id IS NULL AND end_endpoint_type IS NULL "
            "AND end_endpoint_ref_id IS NULL AND target_segment_id IS NULL) OR "
            "(action = 'create' AND start_endpoint_type IS NOT NULL "
            "AND start_endpoint_ref_id IS NOT NULL AND end_endpoint_type IS NOT NULL "
            "AND end_endpoint_ref_id IS NOT NULL AND target_segment_id IS NULL) OR "
            "(action = 'link_existing' AND start_endpoint_type IS NOT NULL "
            "AND start_endpoint_ref_id IS NOT NULL AND end_endpoint_type IS NOT NULL "
            "AND end_endpoint_ref_id IS NOT NULL AND target_segment_id IS NOT NULL)",
            name="ck_fiber_connectivity_action_endpoints",
        ),
        sa.CheckConstraint(
            "start_endpoint_ref_id IS NULL OR end_endpoint_ref_id IS NULL OR "
            "start_endpoint_type <> end_endpoint_type OR "
            "start_endpoint_ref_id <> end_endpoint_ref_id",
            name="ck_fiber_connectivity_distinct_endpoints",
        ),
        sa.CheckConstraint(
            "reviewed_by IS NULL OR reviewed_by <> proposed_by",
            name="ck_fiber_connectivity_separation",
        ),
        sa.CheckConstraint(
            "(status = 'proposed' AND reviewed_by IS NULL AND reviewed_at IS NULL) "
            "OR (status <> 'proposed' AND reviewed_by IS NOT NULL "
            "AND reviewed_at IS NOT NULL)",
            name="ck_fiber_connectivity_review_evidence",
        ),
        sa.CheckConstraint(
            "(status IN ('proposed', 'approved', 'declined') "
            "AND executed_by IS NULL AND executed_at IS NULL) OR "
            "(status NOT IN ('proposed', 'approved', 'declined') "
            "AND executed_by IS NOT NULL AND executed_at IS NOT NULL)",
            name="ck_fiber_connectivity_execution_evidence",
        ),
        sa.CheckConstraint(
            "(status IN ('applied', 'closed') AND finalized_by IS NOT NULL "
            "AND finalized_at IS NOT NULL) OR "
            "(status NOT IN ('applied', 'closed') AND finalized_by IS NULL "
            "AND finalized_at IS NULL)",
            name="ck_fiber_connectivity_finalization_evidence",
        ),
        sa.CheckConstraint(
            "length(feature_content_sha256) = 64",
            name="ck_fiber_connectivity_feature_sha256",
        ),
        sa.CheckConstraint(
            "length(decision_sha256) = 64",
            name="ck_fiber_connectivity_decision_sha256",
        ),
        sa.CheckConstraint(
            "fiber_count IS NULL OR fiber_count > 0",
            name="ck_fiber_connectivity_fiber_count",
        ),
        sa.CheckConstraint(
            "length_m IS NULL OR length_m > 0",
            name="ck_fiber_connectivity_length",
        ),
        sa.ForeignKeyConstraint(
            ["staged_feature_id"],
            ["fiber_topology_staged_features.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_segment_id"], ["fiber_segments.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["segment_change_request_id"],
            ["fiber_change_requests.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["canonical_segment_id"], ["fiber_segments.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "decision_sha256", name="uq_fiber_connectivity_decision_sha256"
        ),
        sa.UniqueConstraint(
            "segment_change_request_id",
            name="uq_fiber_connectivity_segment_request",
        ),
    )
    op.create_index(
        "uq_fiber_connectivity_active_source",
        "fiber_topology_connectivity_decisions",
        ["source_system", "source_asset_type", "source_external_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('proposed', 'approved', 'endpoint_change_requested', "
            "'segment_change_requested')"
        ),
    )
    op.create_index(
        "ix_fiber_connectivity_status",
        "fiber_topology_connectivity_decisions",
        ["status"],
    )
    op.create_index(
        "ix_fiber_connectivity_staged_feature",
        "fiber_topology_connectivity_decisions",
        ["staged_feature_id"],
    )

    op.create_table(
        "fiber_topology_termination_resolutions",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True
        ),
        sa.Column("endpoint_type", sa.String(40), nullable=False),
        sa.Column("endpoint_ref_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("source_decision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("change_request_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("termination_point_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("requested_by", sa.String(160), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'applied', 'rejected')",
            name="ck_fiber_termination_resolution_status",
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND change_request_id IS NOT NULL "
            "AND termination_point_id IS NULL) OR "
            "(status = 'applied' AND termination_point_id IS NOT NULL) OR "
            "(status = 'rejected' AND change_request_id IS NOT NULL "
            "AND termination_point_id IS NULL)",
            name="ck_fiber_termination_resolution_evidence",
        ),
        sa.ForeignKeyConstraint(
            ["source_decision_id"],
            ["fiber_topology_connectivity_decisions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["change_request_id"],
            ["fiber_change_requests.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["termination_point_id"],
            ["fiber_termination_points.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "endpoint_type",
            "endpoint_ref_id",
            name="uq_fiber_termination_resolution_endpoint",
        ),
        sa.UniqueConstraint(
            "change_request_id", name="uq_fiber_termination_resolution_request"
        ),
        sa.UniqueConstraint(
            "termination_point_id", name="uq_fiber_termination_resolution_point"
        ),
    )
    op.create_index(
        "ix_fiber_termination_resolution_status",
        "fiber_topology_termination_resolutions",
        ["status"],
    )
    op.create_foreign_key(
        "fk_fiber_connectivity_start_resolution",
        "fiber_topology_connectivity_decisions",
        "fiber_topology_termination_resolutions",
        ["start_resolution_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_fiber_connectivity_end_resolution",
        "fiber_topology_connectivity_decisions",
        "fiber_topology_termination_resolutions",
        ["end_resolution_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.create_table(
        "fiber_topology_segment_source_links",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True
        ),
        sa.Column("decision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("staged_feature_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_system", sa.String(40), nullable=False),
        sa.Column("source_profile", sa.String(40), nullable=False),
        sa.Column("source_asset_type", sa.String(40), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("segment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("linked_by", sa.String(160), nullable=False),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'retired')",
            name="ck_fiber_segment_source_link_status",
        ),
        sa.CheckConstraint(
            "length(content_sha256) = 64",
            name="ck_fiber_segment_source_link_sha256",
        ),
        sa.ForeignKeyConstraint(
            ["decision_id"],
            ["fiber_topology_connectivity_decisions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["staged_feature_id"],
            ["fiber_topology_staged_features.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["segment_id"], ["fiber_segments.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "source_system",
            "source_asset_type",
            "external_id",
            name="uq_fiber_segment_source_link_identity",
        ),
        sa.UniqueConstraint(
            "decision_id", name="uq_fiber_segment_source_link_decision"
        ),
    )
    op.create_index(
        "ix_fiber_segment_source_link_segment",
        "fiber_topology_segment_source_links",
        ["segment_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_fiber_segment_source_link_segment",
        table_name="fiber_topology_segment_source_links",
    )
    op.drop_table("fiber_topology_segment_source_links")
    op.drop_constraint(
        "fk_fiber_connectivity_end_resolution",
        "fiber_topology_connectivity_decisions",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_fiber_connectivity_start_resolution",
        "fiber_topology_connectivity_decisions",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_fiber_termination_resolution_status",
        table_name="fiber_topology_termination_resolutions",
    )
    op.drop_table("fiber_topology_termination_resolutions")
    op.drop_index(
        "ix_fiber_connectivity_staged_feature",
        table_name="fiber_topology_connectivity_decisions",
    )
    op.drop_index(
        "ix_fiber_connectivity_status",
        table_name="fiber_topology_connectivity_decisions",
    )
    op.drop_index(
        "uq_fiber_connectivity_active_source",
        table_name="fiber_topology_connectivity_decisions",
    )
    op.drop_table("fiber_topology_connectivity_decisions")
    op.drop_index(
        "uq_fiber_termination_active_endpoint",
        table_name="fiber_termination_points",
    )
    # PostgreSQL enum values are intentionally not removed on downgrade.
