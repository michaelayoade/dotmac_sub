"""Add durable ONT electronic-topology observation evidence.

Revision ID: 340_ont_topology_observation_evidence
Revises: 339_ont_assignment_identity_decisions
Create Date: 2026-07-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "340_ont_topology_observation_evidence"
down_revision: str | None = "339_ont_assignment_identity_decisions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ont_topology_observation_evidence",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True
        ),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("evidence_key", sa.String(200), nullable=False),
        sa.Column("observation_sha256", sa.String(64), nullable=False),
        sa.Column("ont_unit_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("observed_olt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("observed_pon_port_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("observed_port_number", sa.Integer(), nullable=True),
        sa.Column("observed_port_label", sa.String(120), nullable=True),
        sa.Column("canonical_olt_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "canonical_pon_port_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("active_assignment_ids", sa.JSON(), nullable=False),
        sa.Column("assignment_conflict_ids", sa.JSON(), nullable=False),
        sa.Column("initial_outcome", sa.String(24), nullable=False),
        sa.Column("latest_outcome", sa.String(24), nullable=False),
        sa.Column("latest_reason", sa.String(500), nullable=True),
        sa.Column("initial_result", sa.JSON(), nullable=False),
        sa.Column("latest_snapshot", sa.JSON(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("seen_count", sa.Integer(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "initial_outcome IN "
            "('initialized', 'confirmed', 'incomplete', 'review_required')",
            name="ck_ont_topology_observation_initial_outcome",
        ),
        sa.CheckConstraint(
            "latest_outcome IN "
            "('initialized', 'confirmed', 'incomplete', 'review_required')",
            name="ck_ont_topology_observation_latest_outcome",
        ),
        sa.CheckConstraint(
            "seen_count > 0",
            name="ck_ont_topology_observation_seen_count",
        ),
        sa.CheckConstraint(
            "length(observation_sha256) = 64",
            name="ck_ont_topology_observation_sha256",
        ),
        sa.ForeignKeyConstraint(["ont_unit_id"], ["ont_units.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["observed_olt_id"], ["olt_devices.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["observed_pon_port_id"], ["pon_ports.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["canonical_olt_id"], ["olt_devices.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["canonical_pon_port_id"], ["pon_ports.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "observation_sha256",
            name="uq_ont_topology_observation_sha256",
        ),
    )
    op.create_index(
        "ix_ont_topology_observation_ont_source",
        "ont_topology_observation_evidence",
        ["ont_unit_id", "source"],
    )
    op.create_index(
        "ix_ont_topology_observation_latest_outcome",
        "ont_topology_observation_evidence",
        ["latest_outcome"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ont_topology_observation_latest_outcome",
        table_name="ont_topology_observation_evidence",
    )
    op.drop_index(
        "ix_ont_topology_observation_ont_source",
        table_name="ont_topology_observation_evidence",
    )
    op.drop_table("ont_topology_observation_evidence")
