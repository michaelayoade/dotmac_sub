"""Add canonical capability-bound event subscriptions and deliveries.

Revision ID: 375_integration_delivery
Revises: 374_integration_capability_sync
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "375_integration_delivery"
down_revision = "374_integration_capability_sync"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "integration_event_subscriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "capability_binding_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("event_type", sa.String(length=160), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("filter_json", sa.JSON(), nullable=False),
        sa.Column("payload_policy_json", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(length=160), nullable=True),
        sa.Column("updated_by", sa.String(length=160), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('disabled', 'enabled')",
            name="ck_integration_event_subscriptions_state",
        ),
        sa.ForeignKeyConstraint(
            ["capability_binding_id"],
            ["integration_capability_bindings.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "capability_binding_id",
            "event_type",
            name="uq_integration_event_subscriptions_binding_event",
        ),
    )
    op.create_index(
        "ix_integration_event_subscriptions_event_state",
        "integration_event_subscriptions",
        ["event_type", "state"],
    )

    op.create_table(
        "integration_deliveries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subscription_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "capability_binding_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("source_event_id", sa.String(length=160), nullable=False),
        sa.Column("event_type", sa.String(length=160), nullable=False),
        sa.Column("destination_key", sa.String(length=240), nullable=False),
        sa.Column("idempotency_key", sa.String(length=240), nullable=False),
        sa.Column("payload_digest", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("leased_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("external_receipt_json", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(length=120), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('pending', 'leased', 'delivered', "
            "'retryable', 'reconciliation_required', 'dead_letter', 'canceled')",
            name="ck_integration_deliveries_state",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name="ck_integration_deliveries_attempt_count"
        ),
        sa.ForeignKeyConstraint(
            ["capability_binding_id"],
            ["integration_capability_bindings.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["integration_event_subscriptions.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_integration_deliveries_idempotency_key"
        ),
    )
    op.create_index(
        "ix_integration_deliveries_state_next_attempt",
        "integration_deliveries",
        ["state", "next_attempt_at"],
    )
    op.create_index(
        "ix_integration_deliveries_binding_created",
        "integration_deliveries",
        ["capability_binding_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("integration_deliveries")
    op.drop_table("integration_event_subscriptions")
