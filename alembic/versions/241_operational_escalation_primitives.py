"""Add operational escalation primitives.

Revision ID: 241_operational_escalation_primitives
Revises: 240_inbox_agent_availability
Create Date: 2026-07-10
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "241_operational_escalation_primitives"
down_revision = "240_inbox_agent_availability"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return

    if not _has_table("operational_owners"):
        op.create_table(
            "operational_owners",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("entity_type", sa.String(length=80), nullable=False),
            sa.Column("entity_id", sa.String(length=100), nullable=False),
            sa.Column("owner_type", sa.String(length=40), nullable=False),
            sa.Column("role", sa.String(length=40), nullable=False),
            sa.Column("service_team_id", postgresql.UUID(as_uuid=True)),
            sa.Column("person_id", postgresql.UUID(as_uuid=True)),
            sa.Column("duty_role", sa.String(length=80)),
            sa.Column("source", sa.String(length=80)),
            sa.Column("reason", sa.Text()),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
            sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text())),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
            sa.ForeignKeyConstraint(["service_team_id"], ["service_teams.id"]),
        )
        op.create_index(
            "ix_operational_owners_entity",
            "operational_owners",
            ["entity_type", "entity_id", "is_active"],
        )
        op.create_index(
            "ix_operational_owners_team",
            "operational_owners",
            ["service_team_id", "is_active"],
        )
        op.create_index(
            "ix_operational_owners_person",
            "operational_owners",
            ["person_id", "is_active"],
        )
        op.create_index(
            "uq_operational_owners_primary_active",
            "operational_owners",
            ["entity_type", "entity_id"],
            unique=True,
            postgresql_where=sa.text("is_active IS TRUE AND role = 'primary'"),
        )

    if not _has_table("operational_watchers"):
        op.create_table(
            "operational_watchers",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("entity_type", sa.String(length=80), nullable=False),
            sa.Column("entity_id", sa.String(length=100), nullable=False),
            sa.Column("watcher_type", sa.String(length=40), nullable=False),
            sa.Column("role", sa.String(length=40), nullable=False),
            sa.Column("service_team_id", postgresql.UUID(as_uuid=True)),
            sa.Column("person_id", postgresql.UUID(as_uuid=True)),
            sa.Column("duty_role", sa.String(length=80)),
            sa.Column("source", sa.String(length=80)),
            sa.Column("reason", sa.Text()),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text())),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
            sa.ForeignKeyConstraint(["service_team_id"], ["service_teams.id"]),
            sa.UniqueConstraint(
                "entity_type",
                "entity_id",
                "watcher_type",
                "service_team_id",
                "person_id",
                "duty_role",
                name="uq_operational_watchers_target",
            ),
        )
        op.create_index(
            "ix_operational_watchers_entity",
            "operational_watchers",
            ["entity_type", "entity_id", "is_active"],
        )
        op.create_index(
            "ix_operational_watchers_team",
            "operational_watchers",
            ["service_team_id", "is_active"],
        )
        op.create_index(
            "ix_operational_watchers_person",
            "operational_watchers",
            ["person_id", "is_active"],
        )

    if not _has_table("operational_room_links"):
        op.create_table(
            "operational_room_links",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("entity_type", sa.String(length=80), nullable=False),
            sa.Column("entity_id", sa.String(length=100), nullable=False),
            sa.Column("provider", sa.String(length=40), nullable=False),
            sa.Column("room_id", sa.String(length=160), nullable=False),
            sa.Column("room_name", sa.String(length=200)),
            sa.Column("room_url", sa.String(length=500)),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text())),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
            sa.UniqueConstraint(
                "entity_type",
                "entity_id",
                "provider",
                "room_id",
                name="uq_operational_room_links_room",
            ),
        )
        op.create_index(
            "ix_operational_room_links_entity",
            "operational_room_links",
            ["entity_type", "entity_id", "is_active"],
        )

    if not _has_table("operational_escalation_policies"):
        op.create_table(
            "operational_escalation_policies",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("name", sa.String(length=160), nullable=False),
            sa.Column("entity_type", sa.String(length=80)),
            sa.Column("scope_type", sa.String(length=80)),
            sa.Column("scope_id", sa.String(length=100)),
            sa.Column("level", sa.Integer(), nullable=False),
            sa.Column("min_severity", sa.String(length=40)),
            sa.Column("min_affected_customers", sa.Integer()),
            sa.Column("vip_only", sa.Boolean(), nullable=False),
            sa.Column("unowned_after_seconds", sa.Integer()),
            sa.Column("stale_owner_update_seconds", sa.Integer()),
            sa.Column("customer_update_due_within_seconds", sa.Integer()),
            sa.Column("unresolved_after_seconds", sa.Integer()),
            sa.Column("channels", postgresql.JSONB(astext_type=sa.Text())),
            sa.Column("cooldown_seconds", sa.Integer(), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text())),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
        )
        op.create_index(
            "ix_operational_escalation_policies_scope",
            "operational_escalation_policies",
            ["entity_type", "scope_type", "scope_id", "is_active"],
        )

    if not _has_table("operational_escalation_events"):
        op.create_table(
            "operational_escalation_events",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("entity_type", sa.String(length=80), nullable=False),
            sa.Column("entity_id", sa.String(length=100), nullable=False),
            sa.Column("policy_id", postgresql.UUID(as_uuid=True)),
            sa.Column("level", sa.Integer(), nullable=False),
            sa.Column("trigger", sa.String(length=80), nullable=False),
            sa.Column("severity", sa.String(length=40)),
            sa.Column("affected_customer_count", sa.Integer()),
            sa.Column("status", sa.String(length=40), nullable=False),
            sa.Column("triggered_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("acknowledged_by_person_id", postgresql.UUID(as_uuid=True)),
            sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
            sa.Column("resolved_at", sa.DateTime(timezone=True)),
            sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text())),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
            sa.ForeignKeyConstraint(
                ["policy_id"], ["operational_escalation_policies.id"]
            ),
        )
        op.create_index(
            "ix_operational_escalation_events_entity",
            "operational_escalation_events",
            ["entity_type", "entity_id", "status"],
        )
        op.create_index(
            "ix_operational_escalation_events_policy",
            "operational_escalation_events",
            ["policy_id", "triggered_at"],
        )

    if not _has_table("operational_escalation_deliveries"):
        op.create_table(
            "operational_escalation_deliveries",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("watcher_id", postgresql.UUID(as_uuid=True)),
            sa.Column("owner_id", postgresql.UUID(as_uuid=True)),
            sa.Column("channel", sa.String(length=40), nullable=False),
            sa.Column("recipient_type", sa.String(length=40), nullable=False),
            sa.Column("recipient_id", sa.String(length=100)),
            sa.Column("recipient_address", sa.String(length=255)),
            sa.Column("delivery_status", sa.String(length=40), nullable=False),
            sa.Column("dedup_key", sa.String(length=255), nullable=False),
            sa.Column("escalation_level", sa.Integer(), nullable=False),
            sa.Column("sent_at", sa.DateTime(timezone=True)),
            sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
            sa.Column("cooldown_until", sa.DateTime(timezone=True)),
            sa.Column("error_message", sa.Text()),
            sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text())),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
            sa.ForeignKeyConstraint(
                ["event_id"],
                ["operational_escalation_events.id"],
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(["watcher_id"], ["operational_watchers.id"]),
            sa.ForeignKeyConstraint(["owner_id"], ["operational_owners.id"]),
            sa.UniqueConstraint(
                "dedup_key", name="uq_operational_escalation_delivery_dedup"
            ),
        )
        op.create_index(
            "ix_operational_escalation_deliveries_event",
            "operational_escalation_deliveries",
            ["event_id", "delivery_status"],
        )
        op.create_index(
            "ix_operational_escalation_deliveries_recipient",
            "operational_escalation_deliveries",
            ["recipient_type", "recipient_id", "channel"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    for table_name in (
        "operational_escalation_deliveries",
        "operational_escalation_events",
        "operational_escalation_policies",
        "operational_room_links",
        "operational_watchers",
        "operational_owners",
    ):
        if _has_table(table_name):
            op.drop_table(table_name)
