"""Add prepaid period-purchase and outage-compensation contracts.

Revision ID: 636_service_period_purchase_contract
Revises: 635_subscription_pause_lifecycle
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "636_service_period_purchase_contract"
down_revision: str | None = "635_subscription_pause_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "prepaid_period_purchases",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.Column("subscription_id", sa.UUID(), nullable=False),
        sa.Column("topup_intent_id", sa.UUID(), nullable=True),
        sa.Column("payment_id", sa.UUID(), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("period_count", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("coverage_starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("coverage_ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("subtotal", sa.Numeric(12, 2), nullable=False),
        sa.Column("tax_total", sa.Numeric(12, 2), nullable=False),
        sa.Column("total", sa.Numeric(12, 2), nullable=False),
        sa.Column("preview_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column(
            "policy_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("created_by", sa.String(length=160), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "period_count >= 1 AND period_count <= 12",
            name="ck_prepaid_period_purchase_count",
        ),
        sa.CheckConstraint(
            "coverage_ends_at > coverage_starts_at",
            name="ck_prepaid_period_purchase_positive_coverage",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["subscribers.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"], ["subscriptions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["topup_intent_id"], ["topup_intents.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["payment_id"], ["payments.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "account_id", "idempotency_key", name="uq_prepaid_period_purchase_key"
        ),
        sa.UniqueConstraint(
            "topup_intent_id", name="uq_prepaid_period_purchase_intent"
        ),
    )
    op.create_index(
        "ix_prepaid_period_purchase_subscription_status",
        "prepaid_period_purchases",
        ["subscription_id", "status"],
    )
    op.create_table(
        "prepaid_period_purchase_periods",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("purchase_id", sa.UUID(), nullable=False),
        sa.Column("subscription_id", sa.UUID(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("unit_price", sa.Numeric(12, 2), nullable=False),
        sa.Column("subtotal", sa.Numeric(12, 2), nullable=False),
        sa.Column("tax_total", sa.Numeric(12, 2), nullable=False),
        sa.Column("total", sa.Numeric(12, 2), nullable=False),
        sa.Column("tax_rate_id", sa.UUID(), nullable=True),
        sa.Column("tax_application", sa.String(length=20), nullable=False),
        sa.Column("invoice_id", sa.UUID(), nullable=True),
        sa.Column("invoice_line_id", sa.UUID(), nullable=True),
        sa.Column("entitlement_id", sa.UUID(), nullable=True),
        sa.Column("preview_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "ordinal >= 1 AND ordinal <= 12", name="ck_prepaid_purchase_period_ordinal"
        ),
        sa.CheckConstraint(
            "ends_at > starts_at", name="ck_prepaid_purchase_period_positive"
        ),
        sa.ForeignKeyConstraint(
            ["purchase_id"], ["prepaid_period_purchases.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"], ["subscriptions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["invoice_id"], ["invoices.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["invoice_line_id"], ["invoice_lines.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["entitlement_id"], ["service_entitlements.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "purchase_id", "ordinal", name="uq_prepaid_purchase_period_ordinal"
        ),
        sa.UniqueConstraint("invoice_id", name="uq_prepaid_purchase_period_invoice"),
        sa.UniqueConstraint(
            "entitlement_id", name="uq_prepaid_purchase_period_entitlement"
        ),
    )
    op.create_index(
        "ix_prepaid_purchase_period_subscription",
        "prepaid_period_purchase_periods",
        ["subscription_id", "starts_at"],
    )
    op.create_table(
        "outage_compensation_decisions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.Column("subscription_id", sa.UUID(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("threshold_seconds", sa.Integer(), nullable=False),
        sa.Column("eligible_seconds", sa.Integer(), nullable=False),
        sa.Column("funded_overlap_seconds", sa.Integer(), nullable=False),
        sa.Column("tail_before", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tail_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("entitlement_id", sa.UUID(), nullable=True),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column(
            "policy_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("preview_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("created_by", sa.String(length=160), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "eligible_seconds >= 0", name="ck_outage_compensation_eligible_seconds"
        ),
        sa.CheckConstraint(
            "funded_overlap_seconds >= 0", name="ck_outage_compensation_funded_seconds"
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["subscribers.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"], ["subscriptions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["entitlement_id"], ["service_entitlements.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_outage_compensation_decision_key"
        ),
    )
    op.create_index(
        "ix_outage_compensation_subscription_created",
        "outage_compensation_decisions",
        ["subscription_id", "created_at"],
    )
    op.create_table(
        "outage_compensation_decision_intervals",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("decision_id", sa.UUID(), nullable=False),
        sa.Column("customer_outage_interval_id", sa.UUID(), nullable=False),
        sa.Column("incident_id", sa.UUID(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("included_seconds", sa.Integer(), nullable=False),
        sa.Column("excluded_seconds", sa.Integer(), nullable=False),
        sa.Column("exclusion_reason", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "included_seconds >= 0", name="ck_outage_compensation_included_seconds"
        ),
        sa.CheckConstraint(
            "excluded_seconds >= 0", name="ck_outage_compensation_excluded_seconds"
        ),
        sa.ForeignKeyConstraint(
            ["decision_id"], ["outage_compensation_decisions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["customer_outage_interval_id"],
            ["customer_outage_intervals.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "customer_outage_interval_id",
            name="uq_outage_compensation_consumed_interval",
        ),
    )
    op.add_column(
        "service_entitlements",
        sa.Column("source_outage_compensation_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_service_entitlements_outage_compensation",
        "service_entitlements",
        "outage_compensation_decisions",
        ["source_outage_compensation_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "uq_service_entitlements_active_outage_compensation",
        "service_entitlements",
        ["source_outage_compensation_id"],
        unique=True,
        postgresql_where=sa.text(
            "status = 'active' AND source_outage_compensation_id IS NOT NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_service_entitlements_active_outage_compensation",
        table_name="service_entitlements",
    )
    op.drop_constraint(
        "fk_service_entitlements_outage_compensation",
        "service_entitlements",
        type_="foreignkey",
    )
    op.drop_column("service_entitlements", "source_outage_compensation_id")
    op.drop_table("outage_compensation_decision_intervals")
    op.drop_index(
        "ix_outage_compensation_subscription_created",
        table_name="outage_compensation_decisions",
    )
    op.drop_table("outage_compensation_decisions")
    op.drop_index(
        "ix_prepaid_purchase_period_subscription",
        table_name="prepaid_period_purchase_periods",
    )
    op.drop_table("prepaid_period_purchase_periods")
    op.drop_index(
        "ix_prepaid_period_purchase_subscription_status",
        table_name="prepaid_period_purchases",
    )
    op.drop_table("prepaid_period_purchases")
