"""Align account lifecycle and communications source-of-truth boundaries.

Revision ID: 277_lifecycle_comms_sot
Revises: 276_financial_import_sot
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "277_lifecycle_comms_sot"
down_revision = "276_financial_import_sot"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {
        str(item["name"])
        for item in sa.inspect(op.get_bind()).get_indexes(table)
        if item.get("name")
    }


def _foreign_keys(table: str) -> set[str]:
    return {
        str(item["name"])
        for item in sa.inspect(op.get_bind()).get_foreign_keys(table)
        if item.get("name")
    }


def _foreign_key_name(table: str, columns: list[str]) -> str | None:
    for item in sa.inspect(op.get_bind()).get_foreign_keys(table):
        if item.get("constrained_columns") == columns:
            name = item.get("name")
            return str(name) if name else None
    return None


def _add_lifecycle_override_columns() -> None:
    existing = _columns("subscribers")
    additions = {
        "lifecycle_override_status": sa.Column(
            "lifecycle_override_status",
            postgresql.ENUM(name="subscriberstatus", create_type=False),
            nullable=True,
        ),
        "lifecycle_override_reason": sa.Column(
            "lifecycle_override_reason", sa.String(200), nullable=True
        ),
        "lifecycle_override_source": sa.Column(
            "lifecycle_override_source", sa.String(120), nullable=True
        ),
        "lifecycle_override_at": sa.Column(
            "lifecycle_override_at", sa.DateTime(timezone=True), nullable=True
        ),
    }
    for name, column in additions.items():
        if name not in existing:
            op.add_column("subscribers", column)


def _create_communication_intents() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "communication_intents" not in tables:
        op.create_table(
            "communication_intents",
            sa.Column(
                "id",
                postgresql.UUID(as_uuid=True),
                primary_key=True,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column(
                "subscriber_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("subscribers.id", ondelete="CASCADE"),
                nullable=True,
            ),
            sa.Column("event_type", sa.String(120), nullable=False),
            sa.Column("category", sa.String(40), nullable=False),
            sa.Column("communication_class", sa.String(40), nullable=False),
            sa.Column(
                "template_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("notification_templates.id"),
                nullable=True,
            ),
            sa.Column("template_code", sa.String(120), nullable=True),
            sa.Column("subject", sa.String(200), nullable=True),
            sa.Column("body", sa.Text(), nullable=True),
            sa.Column(
                "channels", sa.JSON(), nullable=False, server_default=sa.text("'[]'")
            ),
            sa.Column(
                "include_reseller",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            ),
            sa.Column(
                "status", sa.String(40), nullable=False, server_default="pending"
            ),
            sa.Column(
                "suppression_reasons",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            ),
            sa.Column("dedupe_key", sa.String(200), nullable=True),
            sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "metadata",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            ),
            sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
    existing_indexes = _indexes("communication_intents")
    if "ix_communication_intents_subscriber" not in existing_indexes:
        op.create_index(
            "ix_communication_intents_subscriber",
            "communication_intents",
            ["subscriber_id", "created_at"],
        )
    if "ix_communication_intents_status" not in existing_indexes:
        op.create_index(
            "ix_communication_intents_status",
            "communication_intents",
            ["status", "created_at"],
        )
    if "uq_communication_intents_dedupe_key" not in existing_indexes:
        op.create_index(
            "uq_communication_intents_dedupe_key",
            "communication_intents",
            ["dedupe_key"],
            unique=True,
            postgresql_where=sa.text("dedupe_key IS NOT NULL"),
        )


def _align_delivery_lineage() -> None:
    notification_columns = _columns("notifications")
    additions = {
        "communication_intent_id": sa.Column(
            "communication_intent_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        "audience_type": sa.Column("audience_type", sa.String(40), nullable=True),
        "audience_id": sa.Column(
            "audience_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        "metadata": sa.Column(
            "metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'")
        ),
    }
    for name, column in additions.items():
        if name not in notification_columns:
            op.add_column("notifications", column)
    if _foreign_key_name("notifications", ["communication_intent_id"]) is None:
        op.create_foreign_key(
            "fk_notifications_communication_intent_id",
            "notifications",
            "communication_intents",
            ["communication_intent_id"],
            ["id"],
            ondelete="SET NULL",
        )
    if "ix_notifications_communication_intent_id" not in _indexes("notifications"):
        op.create_index(
            "ix_notifications_communication_intent_id",
            "notifications",
            ["communication_intent_id"],
        )

    if "notification_id" not in _columns("inbox_messages"):
        op.add_column(
            "inbox_messages",
            sa.Column("notification_id", postgresql.UUID(as_uuid=True), nullable=True),
        )
    if _foreign_key_name("inbox_messages", ["notification_id"]) is None:
        op.create_foreign_key(
            "fk_inbox_messages_notification_id",
            "inbox_messages",
            "notifications",
            ["notification_id"],
            ["id"],
            ondelete="SET NULL",
        )
    if "ix_inbox_messages_notification_id" not in _indexes("inbox_messages"):
        op.create_index(
            "ix_inbox_messages_notification_id", "inbox_messages", ["notification_id"]
        )


def _backfill() -> None:
    op.execute(
        sa.text(
            """
            UPDATE subscribers s
            SET lifecycle_override_status = s.status,
                lifecycle_override_reason = 'Preserved pre-SOT account state',
                lifecycle_override_source = 'migration:277',
                lifecycle_override_at = now()
            WHERE s.lifecycle_override_status IS NULL
              AND s.status::text <> 'new'
              AND NOT EXISTS (
                  SELECT 1 FROM subscriptions sub WHERE sub.subscriber_id = s.id
              )
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE subscribers s
            SET lifecycle_override_status = s.status,
                lifecycle_override_reason = 'Preserved terminal account/service conflict',
                lifecycle_override_source = 'migration:277',
                lifecycle_override_at = now()
            WHERE s.lifecycle_override_status IS NULL
              AND s.status::text IN ('disabled', 'canceled')
              AND EXISTS (
                  SELECT 1 FROM subscriptions sub
                  WHERE sub.subscriber_id = s.id
                    AND sub.status::text NOT IN (
                        'disabled', 'canceled', 'expired', 'hidden', 'archived'
                    )
              )
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO communication_intents (
                id, subscriber_id, event_type, category, communication_class,
                template_id, subject, body, channels, include_reseller, status,
                suppression_reasons, metadata, scheduled_for, processed_at,
                created_at, updated_at
            )
            SELECT
                n.id, n.subscriber_id,
                COALESCE(n.event_type, 'legacy.notification'),
                COALESCE(n.category, 'general'),
                CASE WHEN n.category = 'marketing' THEN 'marketing'
                     ELSE 'transactional' END,
                n.template_id, n.subject, n.body,
                json_build_array(n.channel::text), false, 'expanded',
                '[]'::json, json_build_object('source', 'migration:277'), n.send_at,
                n.created_at, n.created_at, n.updated_at
            FROM notifications n
            WHERE n.subscriber_id IS NOT NULL
              AND n.communication_intent_id IS NULL
              AND n.status::text IN ('queued', 'sending', 'failed')
            ON CONFLICT (id) DO NOTHING
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE notifications n
            SET communication_intent_id = n.id,
                audience_type = COALESCE(n.audience_type, 'subscriber'),
                audience_id = COALESCE(n.audience_id, n.subscriber_id)
            WHERE EXISTS (
                SELECT 1 FROM communication_intents ci WHERE ci.id = n.id
            )
              AND n.communication_intent_id IS NULL
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO communication_suppressions (
                id, subscriber_id, channel, address, raw_address,
                scope, reason, note, created_at, created_by
            )
            SELECT gen_random_uuid(), bounced.subscriber_id,
                   'email', bounced.normalized_address, bounced.normalized_address,
                   'all', 'bounce', 'Historical hard bounce', now(), 'migration:277'
            FROM (
                SELECT DISTINCT ON (normalized_address)
                       subscriber_id, normalized_address
                FROM (
                    SELECT subscriber_id,
                           lower(trim(recipient)) normalized_address
                    FROM communication_logs
                    WHERE channel::text = 'email' AND status::text = 'bounced'
                      AND recipient IS NOT NULL AND trim(recipient) <> ''
                    UNION ALL
                    SELECT n.subscriber_id, lower(trim(n.recipient))
                    FROM notification_deliveries d
                    JOIN notifications n ON n.id = d.notification_id
                    WHERE n.channel::text = 'email' AND d.status::text = 'bounced'
                      AND n.recipient IS NOT NULL AND trim(n.recipient) <> ''
                ) candidates
                ORDER BY normalized_address, subscriber_id NULLS LAST
            ) bounced
            WHERE NOT EXISTS (
                SELECT 1 FROM communication_suppressions cs
                WHERE cs.channel::text = 'email'
                  AND cs.address = bounced.normalized_address
            )
            """
        )
    )


def upgrade() -> None:
    _add_lifecycle_override_columns()
    _create_communication_intents()
    _align_delivery_lineage()
    _backfill()


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM communication_suppressions WHERE created_by = 'migration:277'"
        )
    )
    if "notification_id" in _columns("inbox_messages"):
        if "ix_inbox_messages_notification_id" in _indexes("inbox_messages"):
            op.drop_index(
                "ix_inbox_messages_notification_id", table_name="inbox_messages"
            )
        inbox_fk = _foreign_key_name("inbox_messages", ["notification_id"])
        if inbox_fk:
            op.drop_constraint(inbox_fk, "inbox_messages", type_="foreignkey")
        op.drop_column("inbox_messages", "notification_id")

    notification_columns = _columns("notifications")
    if "communication_intent_id" in notification_columns:
        if "ix_notifications_communication_intent_id" in _indexes("notifications"):
            op.drop_index(
                "ix_notifications_communication_intent_id", table_name="notifications"
            )
        notification_fk = _foreign_key_name(
            "notifications", ["communication_intent_id"]
        )
        if notification_fk:
            op.drop_constraint(
                notification_fk,
                "notifications",
                type_="foreignkey",
            )
    for column in (
        "metadata",
        "audience_id",
        "audience_type",
        "communication_intent_id",
    ):
        if column in notification_columns:
            op.drop_column("notifications", column)

    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "communication_intents" in tables:
        op.drop_table("communication_intents")

    subscriber_columns = _columns("subscribers")
    for column in (
        "lifecycle_override_at",
        "lifecycle_override_source",
        "lifecycle_override_reason",
        "lifecycle_override_status",
    ):
        if column in subscriber_columns:
            op.drop_column("subscribers", column)
