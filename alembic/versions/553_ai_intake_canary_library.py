"""Add AI Intake canary scenario library and run evidence.

Revision ID: 553_ai_intake_canary_library
Revises: 552_cancel_merged_ticket_sources
Create Date: 2026-08-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "553_ai_intake_canary_library"
down_revision: str | None = "552_cancel_merged_ticket_sources"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json_type() -> sa.JSON:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        return postgresql.JSONB(astext_type=sa.Text())
    return sa.JSON()


def upgrade() -> None:
    json_type = _json_type()
    op.create_table(
        "ai_intake_canary_scenarios",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scenario_key", sa.String(length=120), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("required_for_activation", sa.Boolean(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("tags", json_type, nullable=True),
        sa.Column("current_revision_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_by_person_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_person_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scenario_key", name="uq_ai_intake_canary_scenario_key"),
    )
    op.create_index(
        "ix_ai_intake_canary_scenarios_enabled",
        "ai_intake_canary_scenarios",
        ["enabled", "priority"],
    )

    op.create_table(
        "ai_intake_canary_scenario_revisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scenario_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("definition", json_type, nullable=False),
        sa.Column("definition_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_by_person_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["scenario_id"], ["ai_intake_canary_scenarios.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "scenario_id",
            "revision_number",
            name="uq_ai_intake_canary_scenario_revision_number",
        ),
    )
    op.create_index(
        "ix_ai_intake_canary_scenario_revisions_scenario",
        "ai_intake_canary_scenario_revisions",
        ["scenario_id", "revision_number"],
    )

    op.create_table(
        "ai_intake_canary_suites",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("suite_key", sa.String(length=120), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("required_for_activation", sa.Boolean(), nullable=False),
        sa.Column("created_by_person_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_person_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("suite_key", name="uq_ai_intake_canary_suite_key"),
    )
    op.create_index(
        "ix_ai_intake_canary_suites_enabled",
        "ai_intake_canary_suites",
        ["enabled"],
    )

    op.create_table(
        "ai_intake_canary_suite_scenarios",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("suite_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scenario_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["scenario_id"], ["ai_intake_canary_scenarios.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["suite_id"], ["ai_intake_canary_suites.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "suite_id",
            "scenario_id",
            name="uq_ai_intake_canary_suite_scenario",
        ),
    )
    op.create_index(
        "ix_ai_intake_canary_suite_scenarios_suite",
        "ai_intake_canary_suite_scenarios",
        ["suite_id", "position"],
    )

    op.create_table(
        "ai_intake_canary_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scenario_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "scenario_revision_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("suite_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("policy_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("policy_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("requested_engine", sa.String(length=40), nullable=False),
        sa.Column("actual_engine", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("evidence", json_type, nullable=False),
        sa.Column("created_by_person_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('passed', 'failed')",
            name="ck_ai_intake_canary_runs_status",
        ),
        sa.ForeignKeyConstraint(
            ["policy_id"], ["ai_intake_policies.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["policy_version_id"],
            ["ai_intake_policy_versions.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["scenario_id"], ["ai_intake_canary_scenarios.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["scenario_revision_id"],
            ["ai_intake_canary_scenario_revisions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["suite_id"], ["ai_intake_canary_suites.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ai_intake_canary_runs_scenario_latest",
        "ai_intake_canary_runs",
        ["scenario_id", "created_at"],
    )
    op.create_index(
        "ix_ai_intake_canary_runs_policy_engine",
        "ai_intake_canary_runs",
        ["policy_version_id", "requested_engine", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ai_intake_canary_runs_policy_engine",
        table_name="ai_intake_canary_runs",
    )
    op.drop_index(
        "ix_ai_intake_canary_runs_scenario_latest",
        table_name="ai_intake_canary_runs",
    )
    op.drop_table("ai_intake_canary_runs")
    op.drop_index(
        "ix_ai_intake_canary_suite_scenarios_suite",
        table_name="ai_intake_canary_suite_scenarios",
    )
    op.drop_table("ai_intake_canary_suite_scenarios")
    op.drop_index(
        "ix_ai_intake_canary_suites_enabled",
        table_name="ai_intake_canary_suites",
    )
    op.drop_table("ai_intake_canary_suites")
    op.drop_index(
        "ix_ai_intake_canary_scenario_revisions_scenario",
        table_name="ai_intake_canary_scenario_revisions",
    )
    op.drop_table("ai_intake_canary_scenario_revisions")
    op.drop_index(
        "ix_ai_intake_canary_scenarios_enabled",
        table_name="ai_intake_canary_scenarios",
    )
    op.drop_table("ai_intake_canary_scenarios")
