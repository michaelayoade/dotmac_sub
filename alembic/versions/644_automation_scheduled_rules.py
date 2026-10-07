"""Add cross-module scheduled automation rules and slot idempotency."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "644_automation_scheduled_rules"
down_revision = "643_automation_multi_trigger_conditions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "automation_rule_versions",
        sa.Column("schedule", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_table(
        "automation_scheduled_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rule_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rule_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("slot_key", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "rule_id"],
            ["automation_rules.tenant_id", "automation_rules.id"],
            ondelete="CASCADE",
            name="fk_automation_scheduled_runs_rule_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "rule_version_id"],
            ["automation_rule_versions.tenant_id", "automation_rule_versions.id"],
            ondelete="CASCADE",
            name="fk_automation_scheduled_runs_version_tenant",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "rule_version_id",
            "slot_key",
            name="uq_automation_scheduled_runs_version_slot",
        ),
    )
    op.create_index(
        "ix_automation_scheduled_runs_tenant_created",
        "automation_scheduled_runs",
        ["tenant_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_automation_scheduled_runs_tenant_created",
        table_name="automation_scheduled_runs",
    )
    op.drop_table("automation_scheduled_runs")
    op.drop_column("automation_rule_versions", "schedule")
