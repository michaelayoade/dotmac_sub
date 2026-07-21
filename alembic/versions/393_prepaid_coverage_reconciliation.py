"""Add durable prepaid coverage reconciliation and retire activation setting.

Revision ID: 393_prepaid_coverage_reconciliation
Revises: 392_retire_prepaid_monthly_invoice_owner
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "393_prepaid_coverage_reconciliation"
down_revision = "392_retire_prepaid_monthly_invoice_owner"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "prepaid_coverage_reconciliation_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("preview_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requested_subscription_count", sa.Integer(), nullable=False),
        sa.Column("entitlement_created_count", sa.Integer(), nullable=False),
        sa.Column("already_covered_count", sa.Integer(), nullable=False),
        sa.Column("no_repair_required_count", sa.Integer(), nullable=False),
        sa.Column("quarantined_count", sa.Integer(), nullable=False),
        sa.Column("command_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor", sa.String(length=120), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_prepaid_coverage_reconciliation_runs_idempotency",
        "prepaid_coverage_reconciliation_runs",
        ["idempotency_key"],
        unique=True,
    )
    op.create_index(
        "ix_prepaid_coverage_reconciliation_runs_created_at",
        "prepaid_coverage_reconciliation_runs",
        ["created_at"],
    )
    op.create_table(
        "prepaid_coverage_reconciliation_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subscription_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("reason_code", sa.String(length=80), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column(
            "source_entitlement_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column(
            "source_service_extension_entry_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            "source_invoice_line_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column(
            "source_account_adjustment_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("evidence_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("entitlement_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "decision IN ('entitlement_created', 'already_covered', "
            "'no_repair_required', 'quarantined')",
            name="ck_prepaid_coverage_reconciliation_item_decision",
        ),
        sa.CheckConstraint(
            "source_type IN ('service_entitlement', 'service_extension', "
            "'invoice_line', 'account_adjustment', 'none')",
            name="ck_prepaid_coverage_reconciliation_item_source_type",
        ),
        sa.CheckConstraint(
            "(source_type = 'service_entitlement' AND "
            "source_entitlement_id IS NOT NULL AND "
            "source_service_extension_entry_id IS NULL AND "
            "source_invoice_line_id IS NULL AND "
            "source_account_adjustment_id IS NULL) OR "
            "(source_type = 'service_extension' AND "
            "source_entitlement_id IS NULL AND "
            "source_service_extension_entry_id IS NOT NULL AND "
            "source_invoice_line_id IS NULL AND "
            "source_account_adjustment_id IS NULL) OR "
            "(source_type = 'invoice_line' AND "
            "source_entitlement_id IS NULL AND "
            "source_service_extension_entry_id IS NULL AND "
            "source_invoice_line_id IS NOT NULL AND "
            "source_account_adjustment_id IS NULL) OR "
            "(source_type = 'account_adjustment' AND "
            "source_entitlement_id IS NULL AND "
            "source_service_extension_entry_id IS NULL AND "
            "source_invoice_line_id IS NULL AND "
            "source_account_adjustment_id IS NOT NULL) OR "
            "(source_type = 'none' AND "
            "source_entitlement_id IS NULL AND "
            "source_service_extension_entry_id IS NULL AND "
            "source_invoice_line_id IS NULL AND "
            "source_account_adjustment_id IS NULL)",
            name="ck_prepaid_coverage_reconciliation_item_exact_source",
        ),
        sa.CheckConstraint(
            "ends_at IS NULL OR starts_at IS NOT NULL",
            name="ck_prepaid_coverage_reconciliation_item_period_pair",
        ),
        sa.CheckConstraint(
            "ends_at IS NULL OR ends_at > starts_at",
            name="ck_prepaid_coverage_reconciliation_item_period_order",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["subscribers.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["entitlement_id"], ["service_entitlements.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["source_account_adjustment_id"],
            ["account_adjustments.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_entitlement_id"],
            ["service_entitlements.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_invoice_line_id"],
            ["invoice_lines.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_service_extension_entry_id"],
            ["service_extension_entries.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["prepaid_coverage_reconciliation_runs.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"], ["subscriptions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id",
            "subscription_id",
            name="uq_prepaid_coverage_reconciliation_item_subscription",
        ),
    )
    op.create_index(
        "ix_prepaid_coverage_reconciliation_items_subscription",
        "prepaid_coverage_reconciliation_items",
        ["subscription_id", "created_at"],
    )
    op.create_index(
        "ix_prepaid_coverage_reconciliation_items_reason",
        "prepaid_coverage_reconciliation_items",
        ["decision", "reason_code"],
    )
    op.create_index(
        "ix_prepaid_coverage_reconciliation_items_entitlement",
        "prepaid_coverage_reconciliation_items",
        ["entitlement_id"],
    )
    op.add_column(
        "prepaid_enforcement_readiness",
        sa.Column(
            "coverage_evidence_sha256",
            sa.String(length=64),
            nullable=False,
            server_default="0000000000000000000000000000000000000000000000000000000000000000",
        ),
    )
    op.add_column(
        "prepaid_enforcement_readiness",
        sa.Column(
            "coverage_blocker_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )

    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            DELETE FROM domain_settings
            WHERE domain = 'collections'
              AND key = 'prepaid_enforcement_activation_at'
            """
        )
    )

    if bind.dialect.name == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION prepaid_coverage_reconciliation_append_only()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'prepaid coverage reconciliation evidence is append-only';
            END;
            $$ LANGUAGE plpgsql;

            CREATE TRIGGER prepaid_coverage_reconciliation_runs_append_only
            BEFORE UPDATE OR DELETE ON prepaid_coverage_reconciliation_runs
            FOR EACH ROW EXECUTE FUNCTION prepaid_coverage_reconciliation_append_only();

            CREATE TRIGGER prepaid_coverage_reconciliation_items_append_only
            BEFORE UPDATE OR DELETE ON prepaid_coverage_reconciliation_items
            FOR EACH ROW EXECUTE FUNCTION prepaid_coverage_reconciliation_append_only();
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            """
            DROP TRIGGER IF EXISTS prepaid_coverage_reconciliation_items_append_only
                ON prepaid_coverage_reconciliation_items;
            DROP TRIGGER IF EXISTS prepaid_coverage_reconciliation_runs_append_only
                ON prepaid_coverage_reconciliation_runs;
            DROP FUNCTION IF EXISTS prepaid_coverage_reconciliation_append_only();
            """
        )
    op.drop_column("prepaid_enforcement_readiness", "coverage_blocker_count")
    op.drop_column("prepaid_enforcement_readiness", "coverage_evidence_sha256")
    op.drop_index(
        "ix_prepaid_coverage_reconciliation_items_entitlement",
        table_name="prepaid_coverage_reconciliation_items",
    )
    op.drop_index(
        "ix_prepaid_coverage_reconciliation_items_reason",
        table_name="prepaid_coverage_reconciliation_items",
    )
    op.drop_index(
        "ix_prepaid_coverage_reconciliation_items_subscription",
        table_name="prepaid_coverage_reconciliation_items",
    )
    op.drop_table("prepaid_coverage_reconciliation_items")
    op.drop_index(
        "ix_prepaid_coverage_reconciliation_runs_created_at",
        table_name="prepaid_coverage_reconciliation_runs",
    )
    op.drop_index(
        "uq_prepaid_coverage_reconciliation_runs_idempotency",
        table_name="prepaid_coverage_reconciliation_runs",
    )
    op.drop_table("prepaid_coverage_reconciliation_runs")
