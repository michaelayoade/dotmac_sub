"""Persist intent recipient decisions and shared physical delivery coverage.

Revision ID: 637_comms_delivery_coverage
Revises: 636_allow_name_identified_fiber_topology_features
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "637_comms_delivery_coverage"
down_revision: str | None = "636_allow_name_identified_fiber_topology_features"
branch_labels = None
depends_on = None


def _protect(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY {table}_tenant_isolation ON {table}
          USING (tenant_id = app_current_tenant_id())
          WITH CHECK (tenant_id = app_current_tenant_id())
        """
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO app_user")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO platform_api")


def upgrade() -> None:
    op.create_table(
        "communication_intent_recipients",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("intent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("audience_type", sa.String(length=40), nullable=False),
        sa.Column("audience_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("subscriber_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "channel",
            postgresql.ENUM(name="notificationchannel", create_type=False),
            nullable=False,
        ),
        sa.Column("recipient", sa.String(length=255), nullable=True),
        sa.Column("normalized_recipient", sa.String(length=255), nullable=True),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("suppression_reason", sa.String(length=255), nullable=True),
        sa.Column(
            "requested_status",
            postgresql.ENUM(name="notificationstatus", create_type=False),
            nullable=False,
        ),
        sa.Column("requested_last_error", sa.Text(), nullable=True),
        sa.Column("delivery_latency", sa.String(length=20), nullable=False),
        sa.Column("send_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("canonical_send_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("persist_suppression", sa.Boolean(), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "decision IN ('accepted', 'suppressed')",
            name="ck_communication_intent_recipient_decision",
        ),
        sa.ForeignKeyConstraint(
            ["intent_id"], ["communication_intents.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "id", name="uq_communication_intent_recipients_tenant_id"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "intent_id",
            "audience_type",
            "audience_id",
            "channel",
            "normalized_recipient",
            name="uq_communication_intent_recipient_identity",
        ),
    )
    op.create_index(
        "ix_communication_intent_recipients_tenant_intent",
        "communication_intent_recipients",
        ["tenant_id", "intent_id"],
    )
    _protect("communication_intent_recipients")

    op.create_table(
        "notification_intent_coverage",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("intent_recipient_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("notification_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('reserved', 'covered', 'suppressed')",
            name="ck_notification_intent_coverage_status",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "intent_recipient_id"],
            [
                "communication_intent_recipients.tenant_id",
                "communication_intent_recipients.id",
            ],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["notification_id"], ["notifications.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "intent_recipient_id",
            name="uq_notification_intent_coverage_source",
        ),
    )
    op.create_index(
        "ix_notification_intent_coverage_tenant_delivery",
        "notification_intent_coverage",
        ["tenant_id", "notification_id"],
    )
    _protect("notification_intent_coverage")


def downgrade() -> None:
    for table in ("notification_intent_coverage", "communication_intent_recipients"):
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
        op.drop_table(table)
