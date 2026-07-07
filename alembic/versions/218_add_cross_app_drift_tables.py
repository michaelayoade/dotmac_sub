"""Add cross-app drift detection tables (runs, findings, events, waivers).

A read-only daily control proving CRM / sub / ERP still agree on the business
facts that matter. Detect-only: it persists drift by a stable fingerprint so it
can be tracked new / recurring / resolved / waived, and points humans at the
reconciler that owns the fix. Nothing here heals anything.

Revision ID: 218_add_cross_app_drift_tables
Revises: 217_outage_incident_lifecycle
Create Date: 2026-07-07
"""

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "218_add_cross_app_drift_tables"
down_revision = "217_outage_incident_lifecycle"
branch_labels = None
depends_on = None


def _tables(bind) -> set[str]:
    return set(inspect(bind).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    existing = _tables(bind)

    if "cross_app_drift_runs" not in existing:
        op.create_table(
            "cross_app_drift_runs",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("finished_at", sa.DateTime(timezone=True)),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("checks_run", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "findings_open", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column("findings_new", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "findings_resolved", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column("error", sa.Text()),
        )

    if "cross_app_drift_findings" not in existing:
        op.create_table(
            "cross_app_drift_findings",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("fingerprint", sa.String(64), nullable=False, unique=True),
            sa.Column("check_name", sa.String(80), nullable=False),
            sa.Column("entity_type", sa.String(60), nullable=False),
            sa.Column("canonical_entity_id", sa.String(200), nullable=False),
            sa.Column("mismatch_type", sa.String(80), nullable=False),
            sa.Column("severity", sa.String(20), nullable=False),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("occurrences", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("resolved_at", sa.DateTime(timezone=True)),
            sa.Column("first_run_id", UUID(as_uuid=True)),
            sa.Column("last_run_id", UUID(as_uuid=True)),
            sa.Column("details", sa.JSON()),
        )
        op.create_index(
            "ix_cross_app_drift_findings_status_sev",
            "cross_app_drift_findings",
            ["status", "severity"],
        )
        op.create_index(
            "ix_cross_app_drift_findings_check",
            "cross_app_drift_findings",
            ["check_name"],
        )

    if "cross_app_drift_finding_events" not in existing:
        op.create_table(
            "cross_app_drift_finding_events",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "finding_id",
                UUID(as_uuid=True),
                sa.ForeignKey("cross_app_drift_findings.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("run_id", UUID(as_uuid=True)),
            sa.Column("event_type", sa.String(20), nullable=False),
            sa.Column("at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("snapshot", sa.JSON()),
        )
        op.create_index(
            "ix_cross_app_drift_events_finding",
            "cross_app_drift_finding_events",
            ["finding_id"],
        )

    if "cross_app_drift_waivers" not in existing:
        op.create_table(
            "cross_app_drift_waivers",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("fingerprint", sa.String(64), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("waived_by", sa.String(120)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True)),
            sa.Column(
                "is_active", sa.Boolean(), nullable=False, server_default=sa.true()
            ),
        )
        op.create_index(
            "ix_cross_app_drift_waivers_fp_active",
            "cross_app_drift_waivers",
            ["fingerprint", "is_active"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    existing = _tables(bind)
    for table in (
        "cross_app_drift_finding_events",
        "cross_app_drift_waivers",
        "cross_app_drift_findings",
        "cross_app_drift_runs",
    ):
        if table in existing:
            op.drop_table(table)
