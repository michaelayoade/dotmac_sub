"""Add reviewed fiber source identity decisions and canonical links.

Revision ID: 334_fiber_topology_identity_decisions
Revises: 333_fiber_topology_staging
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "334_fiber_topology_identity_decisions"
down_revision = "333_fiber_topology_staging"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fiber_topology_identity_decisions",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True
        ),
        sa.Column("staged_feature_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("feature_content_sha256", sa.String(64), nullable=False),
        sa.Column("action", sa.String(24), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("target_asset_type", sa.String(40), nullable=True),
        sa.Column("target_asset_id", postgresql.UUID(as_uuid=True), nullable=True),
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
        sa.Column("change_request_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "action IN ('create', 'link_existing', 'reject')",
            name="ck_fiber_topology_identity_decision_action",
        ),
        sa.CheckConstraint(
            "status IN "
            "('proposed', 'approved', 'declined', 'change_requested', "
            "'applied', 'closed')",
            name="ck_fiber_topology_identity_decision_status",
        ),
        sa.CheckConstraint(
            "(action = 'link_existing' AND target_asset_type IS NOT NULL "
            "AND target_asset_id IS NOT NULL) OR "
            "(action <> 'link_existing' AND target_asset_type IS NULL "
            "AND target_asset_id IS NULL)",
            name="ck_fiber_topology_identity_decision_target",
        ),
        sa.CheckConstraint(
            "reviewed_by IS NULL OR reviewed_by <> proposed_by",
            name="ck_fiber_topology_identity_decision_separation",
        ),
        sa.CheckConstraint(
            "(status = 'proposed' AND reviewed_by IS NULL AND reviewed_at IS NULL) "
            "OR (status <> 'proposed' AND reviewed_by IS NOT NULL "
            "AND reviewed_at IS NOT NULL)",
            name="ck_fiber_topology_identity_decision_review_evidence",
        ),
        sa.CheckConstraint(
            "(status IN ('proposed', 'approved', 'declined') "
            "AND executed_by IS NULL "
            "AND executed_at IS NULL) OR "
            "(status NOT IN ('proposed', 'approved', 'declined') "
            "AND executed_by IS NOT NULL "
            "AND executed_at IS NOT NULL)",
            name="ck_fiber_topology_identity_decision_execution_evidence",
        ),
        sa.CheckConstraint(
            "(status IN ('applied', 'closed') AND finalized_by IS NOT NULL "
            "AND finalized_at IS NOT NULL) OR "
            "(status NOT IN ('applied', 'closed') AND finalized_by IS NULL "
            "AND finalized_at IS NULL)",
            name="ck_fiber_topology_identity_decision_finalization_evidence",
        ),
        sa.CheckConstraint(
            "(action = 'create' AND "
            "((status IN ('proposed', 'approved', 'declined') "
            "AND change_request_id IS NULL) "
            "OR (status IN ('change_requested', 'applied', 'closed') "
            "AND change_request_id IS NOT NULL))) OR "
            "(action <> 'create' AND change_request_id IS NULL "
            "AND status <> 'change_requested')",
            name="ck_fiber_topology_identity_decision_change_request",
        ),
        sa.CheckConstraint(
            "(action = 'create') OR "
            "(action = 'link_existing' AND "
            "status IN ('proposed', 'approved', 'declined', 'applied')) OR "
            "(action = 'reject' AND "
            "status IN ('proposed', 'approved', 'declined', 'closed'))",
            name="ck_fiber_topology_identity_decision_action_status",
        ),
        sa.CheckConstraint(
            "length(feature_content_sha256) = 64",
            name="ck_fiber_topology_identity_decision_feature_sha256",
        ),
        sa.CheckConstraint(
            "length(decision_sha256) = 64",
            name="ck_fiber_topology_identity_decision_sha256",
        ),
        sa.ForeignKeyConstraint(
            ["staged_feature_id"],
            ["fiber_topology_staged_features.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["change_request_id"],
            ["fiber_change_requests.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "decision_sha256",
            name="uq_fiber_topology_identity_decision_sha256",
        ),
        sa.UniqueConstraint(
            "change_request_id",
            name="uq_fiber_topology_identity_decision_change_request",
        ),
    )
    op.create_index(
        "ix_fiber_topology_identity_decision_status",
        "fiber_topology_identity_decisions",
        ["status"],
    )
    op.create_index(
        "ix_fiber_topology_identity_decision_action",
        "fiber_topology_identity_decisions",
        ["action"],
    )
    op.create_index(
        "uq_fiber_topology_identity_decision_active_feature",
        "fiber_topology_identity_decisions",
        ["staged_feature_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('proposed', 'approved', 'change_requested')"
        ),
    )

    op.create_table(
        "fiber_topology_asset_source_links",
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
        sa.Column("canonical_asset_type", sa.String(40), nullable=False),
        sa.Column("canonical_asset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("linked_by", sa.String(160), nullable=False),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'retired')",
            name="ck_fiber_topology_source_link_status",
        ),
        sa.CheckConstraint(
            "length(content_sha256) = 64",
            name="ck_fiber_topology_source_link_content_sha256",
        ),
        sa.ForeignKeyConstraint(
            ["decision_id"],
            ["fiber_topology_identity_decisions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["staged_feature_id"],
            ["fiber_topology_staged_features.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "source_system",
            "source_asset_type",
            "external_id",
            name="uq_fiber_topology_source_link_identity",
        ),
        sa.UniqueConstraint(
            "decision_id",
            name="uq_fiber_topology_source_link_decision",
        ),
    )
    op.create_index(
        "ix_fiber_topology_source_link_canonical",
        "fiber_topology_asset_source_links",
        ["canonical_asset_type", "canonical_asset_id"],
    )
    op.create_index(
        "ix_fiber_topology_source_link_status",
        "fiber_topology_asset_source_links",
        ["status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_fiber_topology_source_link_status",
        table_name="fiber_topology_asset_source_links",
    )
    op.drop_index(
        "ix_fiber_topology_source_link_canonical",
        table_name="fiber_topology_asset_source_links",
    )
    op.drop_table("fiber_topology_asset_source_links")
    op.drop_index(
        "ix_fiber_topology_identity_decision_action",
        table_name="fiber_topology_identity_decisions",
    )
    op.drop_index(
        "uq_fiber_topology_identity_decision_active_feature",
        table_name="fiber_topology_identity_decisions",
    )
    op.drop_index(
        "ix_fiber_topology_identity_decision_status",
        table_name="fiber_topology_identity_decisions",
    )
    op.drop_table("fiber_topology_identity_decisions")
