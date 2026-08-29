"""Add immutable reviewed Paystack outside-window recovery evidence.

Revision ID: 568_paystack_recovery_evidence
Revises: 567_inbox_agent_analytics_indexes
Create Date: 2026-08-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "568_paystack_recovery_evidence"
down_revision: str | None = "567_inbox_agent_analytics_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "paystack_outside_window_recovery_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("intent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider_event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("checkout_binding_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("command_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("preview_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("review_reference", sa.String(length=160), nullable=False),
        sa.Column("provider_type", sa.String(length=40), nullable=False),
        sa.Column("provider_reference", sa.String(length=120), nullable=False),
        sa.Column("external_id", sa.String(length=120), nullable=False),
        sa.Column("gross_amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("provider_fee", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column(
            "authorized_net_amount",
            sa.Numeric(precision=12, scale=2),
            nullable=False,
        ),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("disposition", sa.String(length=24), nullable=False),
        sa.Column("command_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor", sa.String(length=120), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "provider_type = 'paystack'",
            name="ck_paystack_outside_window_recovery_provider",
        ),
        sa.CheckConstraint(
            "disposition IN ('recovered', 'linked')",
            name="ck_paystack_outside_window_recovery_disposition",
        ),
        sa.CheckConstraint(
            "gross_amount > 0 AND provider_fee >= 0 "
            "AND provider_fee <= gross_amount "
            "AND authorized_net_amount > 0 "
            "AND authorized_net_amount <= gross_amount",
            name="ck_paystack_outside_window_recovery_money",
        ),
        sa.ForeignKeyConstraint(
            ["checkout_binding_id"],
            ["integration_capability_bindings.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["intent_id"], ["topup_intents.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["payment_id"], ["payments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["provider_event_id"],
            ["payment_provider_events.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["provider_id"], ["payment_providers.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_paystack_outside_window_recovery_idempotency",
        "paystack_outside_window_recovery_runs",
        ["idempotency_key"],
        unique=True,
    )
    op.create_index(
        "ix_paystack_outside_window_recovery_intent_created",
        "paystack_outside_window_recovery_runs",
        ["intent_id", "created_at"],
    )

    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION paystack_recovery_evidence_append_only()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'Paystack recovery evidence is append-only';
            END;
            $$ LANGUAGE plpgsql;

            CREATE TRIGGER paystack_outside_window_recovery_runs_append_only
            BEFORE UPDATE OR DELETE ON paystack_outside_window_recovery_runs
            FOR EACH ROW EXECUTE FUNCTION paystack_recovery_evidence_append_only();
            """
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            DROP TRIGGER IF EXISTS paystack_outside_window_recovery_runs_append_only
                ON paystack_outside_window_recovery_runs;
            DROP FUNCTION IF EXISTS paystack_recovery_evidence_append_only();
            """
        )
    op.drop_index(
        "ix_paystack_outside_window_recovery_intent_created",
        table_name="paystack_outside_window_recovery_runs",
    )
    op.drop_index(
        "uq_paystack_outside_window_recovery_idempotency",
        table_name="paystack_outside_window_recovery_runs",
    )
    op.drop_table("paystack_outside_window_recovery_runs")
