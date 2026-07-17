"""Add operator-scale fiber identity proposal manifests.

Revision ID: 335_fiber_identity_review_batches
Revises: 334_fiber_topology_identity_decisions
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "335_fiber_identity_review_batches"
down_revision = "334_fiber_topology_identity_decisions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fiber_topology_identity_proposal_batches",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True
        ),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("manifest_payload", sa.JSON(), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("source_name", sa.String(255), nullable=False),
        sa.Column("proposed_by", sa.String(160), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(manifest_sha256) = 64",
            name="ck_fiber_topology_identity_proposal_batch_sha256",
        ),
        sa.CheckConstraint(
            "length(request_sha256) = 64",
            name="ck_fiber_topology_identity_proposal_batch_request_sha256",
        ),
        sa.CheckConstraint(
            "item_count > 0",
            name="ck_fiber_topology_identity_proposal_batch_item_count",
        ),
        sa.UniqueConstraint(
            "manifest_sha256",
            name="uq_fiber_topology_identity_proposal_batch_manifest",
        ),
        sa.UniqueConstraint(
            "request_sha256",
            name="uq_fiber_topology_identity_proposal_batch_request",
        ),
    )
    op.create_index(
        "ix_fiber_topology_identity_proposal_batch_created",
        "fiber_topology_identity_proposal_batches",
        ["created_at"],
    )

    op.add_column(
        "fiber_topology_identity_decisions",
        sa.Column("source_system", sa.String(40), nullable=True),
    )
    op.add_column(
        "fiber_topology_identity_decisions",
        sa.Column("source_asset_type", sa.String(40), nullable=True),
    )
    op.add_column(
        "fiber_topology_identity_decisions",
        sa.Column("source_external_id", sa.String(255), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE fiber_topology_identity_decisions AS decision "
            "SET source_system = batch.source_system, "
            "source_asset_type = feature.asset_type, "
            "source_external_id = feature.external_id "
            "FROM fiber_topology_staged_features AS feature "
            "JOIN fiber_topology_source_batches AS batch "
            "ON batch.id = feature.batch_id "
            "WHERE decision.staged_feature_id = feature.id"
        )
    )
    op.alter_column(
        "fiber_topology_identity_decisions",
        "source_system",
        existing_type=sa.String(40),
        nullable=False,
    )
    op.alter_column(
        "fiber_topology_identity_decisions",
        "source_asset_type",
        existing_type=sa.String(40),
        nullable=False,
    )
    op.create_index(
        "uq_fiber_topology_identity_decision_active_source",
        "fiber_topology_identity_decisions",
        ["source_system", "source_asset_type", "source_external_id"],
        unique=True,
        postgresql_where=sa.text(
            "source_external_id IS NOT NULL AND "
            "status IN ('proposed', 'approved', 'change_requested')"
        ),
    )

    op.add_column(
        "fiber_topology_identity_decisions",
        sa.Column("proposal_batch_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "fiber_topology_identity_decisions",
        sa.Column("proposal_batch_row_number", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_fiber_topology_identity_decision_proposal_batch",
        "fiber_topology_identity_decisions",
        "fiber_topology_identity_proposal_batches",
        ["proposal_batch_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_fiber_topology_identity_decision_batch_evidence",
        "fiber_topology_identity_decisions",
        "(proposal_batch_id IS NULL AND proposal_batch_row_number IS NULL) OR "
        "(proposal_batch_id IS NOT NULL AND proposal_batch_row_number > 0)",
    )
    op.create_unique_constraint(
        "uq_fiber_topology_identity_decision_batch_row",
        "fiber_topology_identity_decisions",
        ["proposal_batch_id", "proposal_batch_row_number"],
    )
    op.create_index(
        "ix_fiber_topology_identity_decision_batch",
        "fiber_topology_identity_decisions",
        ["proposal_batch_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_fiber_topology_identity_decision_batch",
        table_name="fiber_topology_identity_decisions",
    )
    op.drop_constraint(
        "uq_fiber_topology_identity_decision_batch_row",
        "fiber_topology_identity_decisions",
        type_="unique",
    )
    op.drop_constraint(
        "ck_fiber_topology_identity_decision_batch_evidence",
        "fiber_topology_identity_decisions",
        type_="check",
    )
    op.drop_constraint(
        "fk_fiber_topology_identity_decision_proposal_batch",
        "fiber_topology_identity_decisions",
        type_="foreignkey",
    )
    op.drop_column("fiber_topology_identity_decisions", "proposal_batch_row_number")
    op.drop_column("fiber_topology_identity_decisions", "proposal_batch_id")

    op.drop_index(
        "uq_fiber_topology_identity_decision_active_source",
        table_name="fiber_topology_identity_decisions",
    )
    op.drop_column("fiber_topology_identity_decisions", "source_external_id")
    op.drop_column("fiber_topology_identity_decisions", "source_asset_type")
    op.drop_column("fiber_topology_identity_decisions", "source_system")

    op.drop_index(
        "ix_fiber_topology_identity_proposal_batch_created",
        table_name="fiber_topology_identity_proposal_batches",
    )
    op.drop_table("fiber_topology_identity_proposal_batches")
