"""Add native campaigns, AI operations, and workqueue primitives.

Revision ID: 247_campaign_ai_workqueue
Revises: 246_inbox_complete_ops
Create Date: 2026-07-10
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "247_campaign_ai_workqueue"
down_revision = "246_inbox_complete_ops"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def _has_index(table_name: str, index_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return index_name in {
        index["name"] for index in inspect(op.get_bind()).get_indexes(table_name)
    }


def _json_type() -> sa.types.TypeEngine:
    if op.get_bind().dialect.name == "postgresql":
        return postgresql.JSONB(astext_type=sa.Text())
    return sa.JSON()


def _uuid_type() -> sa.types.TypeEngine:
    if op.get_bind().dialect.name == "postgresql":
        return postgresql.UUID(as_uuid=True)
    return sa.String(length=36)


def _create_index_once(
    table_name: str,
    index_name: str,
    columns: list[str],
    *,
    unique: bool = False,
    postgresql_where=None,
) -> None:
    if _has_table(table_name) and not _has_index(table_name, index_name):
        op.create_index(
            index_name,
            table_name,
            columns,
            unique=unique,
            postgresql_where=postgresql_where,
        )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return

    uuid_type = _uuid_type()
    json_type = _json_type()

    if not _has_table("campaign_senders"):
        op.create_table(
            "campaign_senders",
            sa.Column("id", uuid_type, primary_key=True),
            sa.Column("name", sa.String(length=160), nullable=False),
            sa.Column("sender_key", sa.String(length=120), nullable=False),
            sa.Column("from_name", sa.String(length=160)),
            sa.Column("from_email", sa.String(length=255)),
            sa.Column("reply_to", sa.String(length=255)),
            sa.Column(
                "is_active", sa.Boolean(), nullable=False, server_default=sa.true()
            ),
            sa.Column("metadata", json_type),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
            sa.UniqueConstraint("sender_key", name="uq_campaign_senders_sender_key"),
        )
    _create_index_once(
        "campaign_senders",
        "ix_campaign_senders_active",
        ["is_active", "name"],
    )

    if not _has_table("campaigns"):
        op.create_table(
            "campaigns",
            sa.Column("id", uuid_type, primary_key=True),
            sa.Column("crm_campaign_id", uuid_type),
            sa.Column("name", sa.String(length=200), nullable=False),
            sa.Column(
                "campaign_type",
                sa.String(length=40),
                nullable=False,
                server_default="one_time",
            ),
            sa.Column(
                "channel",
                sa.String(length=40),
                nullable=False,
                server_default="email",
            ),
            sa.Column(
                "status",
                sa.String(length=40),
                nullable=False,
                server_default="draft",
            ),
            sa.Column("subject", sa.String(length=200)),
            sa.Column("body_html", sa.Text()),
            sa.Column("body_text", sa.Text()),
            sa.Column("from_name", sa.String(length=160)),
            sa.Column("from_email", sa.String(length=255)),
            sa.Column("reply_to", sa.String(length=255)),
            sa.Column("whatsapp_template_name", sa.String(length=200)),
            sa.Column("whatsapp_template_language", sa.String(length=10)),
            sa.Column("whatsapp_template_components", json_type),
            sa.Column("segment_filter", json_type),
            sa.Column("scheduled_at", sa.DateTime(timezone=True)),
            sa.Column("sending_started_at", sa.DateTime(timezone=True)),
            sa.Column("completed_at", sa.DateTime(timezone=True)),
            sa.Column(
                "total_recipients", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column("sent_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "delivered_count", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("opened_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "clicked_count", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column("created_by_system_user_id", uuid_type),
            sa.Column("campaign_sender_id", uuid_type),
            sa.Column("service_team_id", uuid_type),
            sa.Column("connector_config_id", uuid_type),
            sa.Column(
                "is_active", sa.Boolean(), nullable=False, server_default=sa.true()
            ),
            sa.Column("metadata", json_type),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
            sa.ForeignKeyConstraint(["created_by_system_user_id"], ["system_users.id"]),
            sa.ForeignKeyConstraint(["campaign_sender_id"], ["campaign_senders.id"]),
            sa.ForeignKeyConstraint(["service_team_id"], ["service_teams.id"]),
            sa.ForeignKeyConstraint(["connector_config_id"], ["connector_configs.id"]),
        )
    _create_index_once(
        "campaigns", "ix_campaigns_status_scheduled", ["status", "scheduled_at"]
    )
    _create_index_once(
        "campaigns", "ix_campaigns_channel_status", ["channel", "status"]
    )
    _create_index_once(
        "campaigns", "ix_campaigns_created_by", ["created_by_system_user_id"]
    )
    _create_index_once(
        "campaigns",
        "uq_campaigns_crm_campaign_id",
        ["crm_campaign_id"],
        unique=True,
        postgresql_where=sa.text("crm_campaign_id IS NOT NULL"),
    )

    if not _has_table("campaign_steps"):
        op.create_table(
            "campaign_steps",
            sa.Column("id", uuid_type, primary_key=True),
            sa.Column("campaign_id", uuid_type, nullable=False),
            sa.Column("step_index", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("name", sa.String(length=200)),
            sa.Column("subject", sa.String(length=200)),
            sa.Column("body_html", sa.Text()),
            sa.Column("body_text", sa.Text()),
            sa.Column("delay_days", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
            sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"]),
            sa.UniqueConstraint(
                "campaign_id",
                "step_index",
                name="uq_campaign_steps_index",
            ),
        )

    if not _has_table("campaign_recipients"):
        op.create_table(
            "campaign_recipients",
            sa.Column("id", uuid_type, primary_key=True),
            sa.Column("campaign_id", uuid_type, nullable=False),
            sa.Column("subscriber_id", uuid_type, nullable=False),
            sa.Column("step_id", uuid_type),
            sa.Column("address", sa.String(length=255), nullable=False),
            sa.Column("email", sa.String(length=255)),
            sa.Column(
                "status",
                sa.String(length=40),
                nullable=False,
                server_default="pending",
            ),
            sa.Column("notification_id", uuid_type),
            sa.Column("conversation_id", uuid_type),
            sa.Column("message_id", uuid_type),
            sa.Column("sent_at", sa.DateTime(timezone=True)),
            sa.Column("delivered_at", sa.DateTime(timezone=True)),
            sa.Column("failed_reason", sa.Text()),
            sa.Column("opened_at", sa.DateTime(timezone=True)),
            sa.Column("clicked_at", sa.DateTime(timezone=True)),
            sa.Column("open_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("click_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("metadata", json_type),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"]),
            sa.ForeignKeyConstraint(["subscriber_id"], ["subscribers.id"]),
            sa.ForeignKeyConstraint(["step_id"], ["campaign_steps.id"]),
            sa.ForeignKeyConstraint(["notification_id"], ["notifications.id"]),
            sa.ForeignKeyConstraint(["conversation_id"], ["inbox_conversations.id"]),
            sa.ForeignKeyConstraint(["message_id"], ["inbox_messages.id"]),
            sa.UniqueConstraint(
                "campaign_id",
                "subscriber_id",
                "step_id",
                name="uq_campaign_sub_step",
            ),
        )
    _create_index_once(
        "campaign_recipients",
        "uq_campaign_sub_null_step",
        ["campaign_id", "subscriber_id"],
        unique=True,
        postgresql_where=sa.text("step_id IS NULL"),
    )
    _create_index_once(
        "campaign_recipients",
        "ix_campaign_recipients_status",
        ["campaign_id", "status"],
    )
    _create_index_once(
        "campaign_recipients",
        "ix_campaign_recipients_subscriber",
        ["subscriber_id"],
    )
    _create_index_once(
        "campaign_recipients",
        "ix_campaign_recipients_conversation",
        ["conversation_id"],
    )

    if not _has_table("ai_insights"):
        op.create_table(
            "ai_insights",
            sa.Column("id", uuid_type, primary_key=True),
            sa.Column("persona_key", sa.String(length=80), nullable=False),
            sa.Column("domain", sa.String(length=80), nullable=False),
            sa.Column(
                "severity", sa.String(length=40), nullable=False, server_default="info"
            ),
            sa.Column(
                "status", sa.String(length=40), nullable=False, server_default="pending"
            ),
            sa.Column("entity_type", sa.String(length=80), nullable=False),
            sa.Column("entity_id", sa.String(length=120)),
            sa.Column("title", sa.String(length=300), nullable=False),
            sa.Column("summary", sa.Text(), nullable=False),
            sa.Column("structured_output", json_type),
            sa.Column("confidence_score", sa.Numeric(3, 2)),
            sa.Column("recommendations", json_type),
            sa.Column("context_quality_score", sa.Numeric(3, 2)),
            sa.Column(
                "llm_provider",
                sa.String(length=40),
                nullable=False,
                server_default="native",
            ),
            sa.Column("llm_model", sa.String(length=100)),
            sa.Column("llm_tokens_in", sa.Integer()),
            sa.Column("llm_tokens_out", sa.Integer()),
            sa.Column("llm_endpoint", sa.String(length=20)),
            sa.Column("generation_time_ms", sa.Integer()),
            sa.Column("trigger", sa.String(length=40), nullable=False),
            sa.Column("triggered_by_system_user_id", uuid_type),
            sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
            sa.Column("acknowledged_by_system_user_id", uuid_type),
            sa.Column("expires_at", sa.DateTime(timezone=True)),
            sa.Column("metadata", json_type),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
            sa.ForeignKeyConstraint(
                ["triggered_by_system_user_id"], ["system_users.id"]
            ),
            sa.ForeignKeyConstraint(
                ["acknowledged_by_system_user_id"], ["system_users.id"]
            ),
        )
    for index_name, columns in {
        "ix_ai_insights_domain_status": ["domain", "status"],
        "ix_ai_insights_entity": ["entity_type", "entity_id"],
        "ix_ai_insights_persona": ["persona_key"],
        "ix_ai_insights_created": ["created_at"],
        "ix_ai_insights_severity": ["severity"],
    }.items():
        _create_index_once("ai_insights", index_name, columns)

    if not _has_table("ai_intake_configs"):
        op.create_table(
            "ai_intake_configs",
            sa.Column("id", uuid_type, primary_key=True),
            sa.Column("scope_key", sa.String(length=160), nullable=False),
            sa.Column("channel_type", sa.String(length=40), nullable=False),
            sa.Column(
                "is_enabled", sa.Boolean(), nullable=False, server_default=sa.false()
            ),
            sa.Column(
                "confidence_threshold",
                sa.Float(),
                nullable=False,
                server_default="0.75",
            ),
            sa.Column(
                "allow_followup_questions",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            ),
            sa.Column(
                "max_clarification_turns",
                sa.Integer(),
                nullable=False,
                server_default="1",
            ),
            sa.Column(
                "escalate_after_minutes",
                sa.Integer(),
                nullable=False,
                server_default="5",
            ),
            sa.Column(
                "exclude_campaign_attribution",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            ),
            sa.Column("fallback_team_id", uuid_type),
            sa.Column("instructions", sa.Text()),
            sa.Column("department_mappings", json_type),
            sa.Column("metadata", json_type),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
            sa.UniqueConstraint("scope_key", name="uq_ai_intake_configs_scope_key"),
        )

    if not _has_table("workqueue_snoozes"):
        op.create_table(
            "workqueue_snoozes",
            sa.Column("id", uuid_type, primary_key=True),
            sa.Column("user_id", uuid_type, nullable=False),
            sa.Column("item_kind", sa.String(length=32), nullable=False),
            sa.Column("item_id", uuid_type, nullable=False),
            sa.Column("snooze_until", sa.DateTime(timezone=True)),
            sa.Column(
                "until_next_reply",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "user_id",
                "item_kind",
                "item_id",
                name="uq_workqueue_snooze_user_item",
            ),
        )
    _create_index_once("workqueue_snoozes", "ix_workqueue_snoozes_user_id", ["user_id"])
    _create_index_once(
        "workqueue_snoozes",
        "ix_workqueue_snooze_user_until",
        ["user_id", "snooze_until"],
    )
    _create_index_once(
        "workqueue_snoozes",
        "ix_workqueue_snooze_item",
        ["item_kind", "item_id"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    for table_name in (
        "workqueue_snoozes",
        "ai_intake_configs",
        "ai_insights",
        "campaign_recipients",
        "campaign_steps",
        "campaigns",
        "campaign_senders",
    ):
        if _has_table(table_name):
            op.drop_table(table_name)
