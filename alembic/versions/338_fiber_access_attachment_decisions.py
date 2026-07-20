"""Add reviewed fiber access attachment decisions.

Revision ID: 338_fiber_access_attachment_decisions
Revises: 337_fiber_topology_connectivity_decisions
Create Date: 2026-07-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "338_fiber_access_attachment_decisions"
down_revision: str | None = "337_fiber_topology_connectivity_decisions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "fiber_access_attachment_decisions",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True
        ),
        sa.Column("attachment_type", sa.String(20), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("subject_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "target_splitter_port_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            "previous_splitter_port_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("olt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("pon_port_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("splitter_id", postgresql.UUID(as_uuid=True), nullable=False),
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
            "attachment_type IN ('pon_input', 'ont_output')",
            name="ck_fiber_access_attachment_type",
        ),
        sa.CheckConstraint(
            "action IN ('attach', 'detach')",
            name="ck_fiber_access_attachment_action",
        ),
        sa.CheckConstraint(
            "status IN ('proposed', 'approved', 'declined', 'applied', 'closed')",
            name="ck_fiber_access_attachment_status",
        ),
        sa.CheckConstraint(
            "(action = 'attach' AND target_splitter_port_id IS NOT NULL) OR "
            "(action = 'detach' AND target_splitter_port_id IS NULL "
            "AND previous_splitter_port_id IS NOT NULL)",
            name="ck_fiber_access_attachment_target",
        ),
        sa.CheckConstraint(
            "reviewed_by IS NULL OR reviewed_by <> proposed_by",
            name="ck_fiber_access_attachment_separation",
        ),
        sa.CheckConstraint(
            "(status = 'proposed' AND reviewed_by IS NULL AND reviewed_at IS NULL) OR "
            "(status <> 'proposed' AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)",
            name="ck_fiber_access_attachment_review_evidence",
        ),
        sa.CheckConstraint(
            "(status IN ('applied', 'closed') AND executed_by IS NOT NULL "
            "AND executed_at IS NOT NULL AND result_payload IS NOT NULL "
            "AND result_sha256 IS NOT NULL) OR "
            "(status NOT IN ('applied', 'closed') AND executed_by IS NULL "
            "AND executed_at IS NULL AND result_payload IS NULL "
            "AND result_sha256 IS NULL)",
            name="ck_fiber_access_attachment_result_evidence",
        ),
        sa.CheckConstraint(
            "length(decision_sha256) = 64",
            name="ck_fiber_access_attachment_decision_sha256",
        ),
        sa.CheckConstraint(
            "result_sha256 IS NULL OR length(result_sha256) = 64",
            name="ck_fiber_access_attachment_result_sha256",
        ),
        sa.ForeignKeyConstraint(
            ["target_splitter_port_id"],
            ["splitter_ports.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["previous_splitter_port_id"],
            ["splitter_ports.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["olt_id"], ["olt_devices.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["pon_port_id"], ["pon_ports.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["splitter_id"], ["splitters.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "decision_sha256",
            name="uq_fiber_access_attachment_decision_sha256",
        ),
    )
    op.create_index(
        "uq_fiber_access_attachment_active_subject",
        "fiber_access_attachment_decisions",
        ["attachment_type", "subject_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('proposed', 'approved')"),
    )
    op.create_index(
        "ix_fiber_access_attachment_status",
        "fiber_access_attachment_decisions",
        ["status"],
    )
    op.create_index(
        "uq_pon_port_splitter_links_active_input",
        "pon_port_splitter_links",
        ["splitter_port_id"],
        unique=True,
        postgresql_where=sa.text("active"),
    )
    op.create_index(
        "uq_ont_units_active_splitter_port",
        "ont_units",
        ["splitter_port_id"],
        unique=True,
        postgresql_where=sa.text("is_active AND splitter_port_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_ont_units_active_splitter_port",
        table_name="ont_units",
    )
    op.drop_index(
        "uq_pon_port_splitter_links_active_input",
        table_name="pon_port_splitter_links",
    )
    op.drop_index(
        "ix_fiber_access_attachment_status",
        table_name="fiber_access_attachment_decisions",
    )
    op.drop_index(
        "uq_fiber_access_attachment_active_subject",
        table_name="fiber_access_attachment_decisions",
    )
    op.drop_table("fiber_access_attachment_decisions")
