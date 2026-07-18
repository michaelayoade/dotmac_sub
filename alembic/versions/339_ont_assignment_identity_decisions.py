"""Add reviewed ONT assignment identity repair decisions.

Revision ID: 339_ont_assignment_identity_decisions
Revises: 338_fiber_access_attachment_decisions
Create Date: 2026-07-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "339_ont_assignment_identity_decisions"
down_revision: str | None = "338_fiber_access_attachment_decisions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ont_assignment_identity_decisions",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True
        ),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column(
            "primary_assignment_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("ont_unit_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "target_subscription_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("target_subscriber_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("target_pon_port_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("target_olt_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("duplicate_assignment_ids", sa.JSON(), nullable=False),
        sa.Column("input_snapshot", sa.JSON(), nullable=False),
        sa.Column("input_sha256", sa.String(64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("decision_sha256", sa.String(64), nullable=False),
        sa.Column("proposed_by", sa.String(160), nullable=False),
        sa.Column("proposed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_by", sa.String(160), nullable=True),
        sa.Column("review_notes", sa.Text(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("executed_by", sa.String(160), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_reason", sa.Text(), nullable=True),
        sa.Column("result_payload", sa.JSON(), nullable=True),
        sa.Column("result_sha256", sa.String(64), nullable=True),
        sa.CheckConstraint(
            "action IN ('canonicalize', 'deactivate')",
            name="ck_ont_assignment_identity_action",
        ),
        sa.CheckConstraint(
            "status IN ('proposed', 'approved', 'declined', 'applied', 'closed')",
            name="ck_ont_assignment_identity_status",
        ),
        sa.CheckConstraint(
            "(action = 'canonicalize' AND target_subscription_id IS NOT NULL "
            "AND target_subscriber_id IS NOT NULL AND target_pon_port_id IS NOT NULL "
            "AND target_olt_id IS NOT NULL) OR "
            "(action = 'deactivate' AND target_subscription_id IS NULL "
            "AND target_subscriber_id IS NULL AND target_pon_port_id IS NULL "
            "AND target_olt_id IS NULL)",
            name="ck_ont_assignment_identity_targets",
        ),
        sa.CheckConstraint(
            "reviewed_by IS NULL OR reviewed_by <> proposed_by",
            name="ck_ont_assignment_identity_separation",
        ),
        sa.CheckConstraint(
            "(status = 'proposed' AND reviewed_by IS NULL AND reviewed_at IS NULL) OR "
            "(status <> 'proposed' AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)",
            name="ck_ont_assignment_identity_review_evidence",
        ),
        sa.CheckConstraint(
            "(status IN ('applied', 'closed') AND executed_by IS NOT NULL "
            "AND executed_at IS NOT NULL AND result_payload IS NOT NULL "
            "AND result_sha256 IS NOT NULL) OR "
            "(status NOT IN ('applied', 'closed') AND executed_by IS NULL "
            "AND executed_at IS NULL AND result_payload IS NULL "
            "AND result_sha256 IS NULL)",
            name="ck_ont_assignment_identity_result_evidence",
        ),
        sa.CheckConstraint(
            "length(input_sha256) = 64",
            name="ck_ont_assignment_identity_input_sha256",
        ),
        sa.CheckConstraint(
            "length(decision_sha256) = 64",
            name="ck_ont_assignment_identity_decision_sha256",
        ),
        sa.CheckConstraint(
            "result_sha256 IS NULL OR length(result_sha256) = 64",
            name="ck_ont_assignment_identity_result_sha256",
        ),
        sa.ForeignKeyConstraint(
            ["primary_assignment_id"],
            ["ont_assignments.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["ont_unit_id"], ["ont_units.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["target_subscription_id"],
            ["subscriptions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_subscriber_id"],
            ["subscribers.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_pon_port_id"], ["pon_ports.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["target_olt_id"], ["olt_devices.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "decision_sha256",
            name="uq_ont_assignment_identity_decision_sha256",
        ),
    )
    op.create_index(
        "uq_ont_assignment_identity_active_primary",
        "ont_assignment_identity_decisions",
        ["primary_assignment_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('proposed', 'approved')"),
    )
    op.create_index(
        "ix_ont_assignment_identity_status",
        "ont_assignment_identity_decisions",
        ["status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ont_assignment_identity_status",
        table_name="ont_assignment_identity_decisions",
    )
    op.drop_index(
        "uq_ont_assignment_identity_active_primary",
        table_name="ont_assignment_identity_decisions",
    )
    op.drop_table("ont_assignment_identity_decisions")
