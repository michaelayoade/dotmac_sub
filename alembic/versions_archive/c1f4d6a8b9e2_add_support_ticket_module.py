"""add support ticket module

Revision ID: c1f4d6a8b9e2
Revises: 9a1009941676
Create Date: 2026-03-10 09:10:00.000000
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision = "c1f4d6a8b9e2"
down_revision = "9a1009941676"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ticket_status = sa.Enum(
        "new",
        "open",
        "pending",
        "waiting_on_customer",
        "lastmile_rerun",
        "site_under_construction",
        "on_hold",
        "resolved",
        "closed",
        "canceled",
        "merged",
        name="ticketstatus",
    )
    ticket_priority = sa.Enum(
        "lower",
        "low",
        "medium",
        "normal",
        "high",
        "urgent",
        name="ticketpriority",
    )
    ticket_channel = sa.Enum("web", "email", "phone", "chat", "api", name="ticketchannel")

    bind = op.get_bind()
    ticket_status.create(bind, checkfirst=True)
    ticket_priority.create(bind, checkfirst=True)
    ticket_channel.create(bind, checkfirst=True)

    ticket_status_ref = postgresql.ENUM(name="ticketstatus", create_type=False)
    ticket_priority_ref = postgresql.ENUM(name="ticketpriority", create_type=False)
    ticket_channel_ref = postgresql.ENUM(name="ticketchannel", create_type=False)

    op.create_table(
        "support_tickets",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subscriber_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("customer_account_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("customer_person_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_by_person_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("assigned_to_person_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("technician_person_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("ticket_manager_person_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("site_coordinator_person_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("service_team_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("number", sa.String(length=50), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("region", sa.String(length=80), nullable=True),
        sa.Column("status", ticket_status_ref, nullable=False),
        sa.Column("priority", ticket_priority_ref, nullable=False),
        sa.Column("ticket_type", sa.String(length=80), nullable=True),
        sa.Column("channel", ticket_channel_ref, nullable=False),
        sa.Column("tags", sa.JSON(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("attachments", sa.JSON(), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("merged_into_ticket_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["assigned_to_person_id"], ["subscribers.id"]),
        sa.ForeignKeyConstraint(["created_by_person_id"], ["subscribers.id"]),
        sa.ForeignKeyConstraint(["customer_account_id"], ["subscribers.id"]),
        sa.ForeignKeyConstraint(["customer_person_id"], ["subscribers.id"]),
        sa.ForeignKeyConstraint(["merged_into_ticket_id"], ["support_tickets.id"]),
        sa.ForeignKeyConstraint(["site_coordinator_person_id"], ["subscribers.id"]),
        sa.ForeignKeyConstraint(["subscriber_id"], ["subscribers.id"]),
        sa.ForeignKeyConstraint(["technician_person_id"], ["subscribers.id"]),
        sa.ForeignKeyConstraint(["ticket_manager_person_id"], ["subscribers.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("number"),
    )
    op.create_index("ix_support_tickets_number", "support_tickets", ["number"])
    op.create_index("ix_support_tickets_status", "support_tickets", ["status"])
    op.create_index("ix_support_tickets_priority", "support_tickets", ["priority"])
    op.create_index("ix_support_tickets_subscriber", "support_tickets", ["subscriber_id"])
    op.create_index("ix_support_tickets_active", "support_tickets", ["is_active"])

    op.create_table(
        "support_ticket_assignees",
        sa.Column("ticket_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("person_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["person_id"], ["subscribers.id"]),
        sa.ForeignKeyConstraint(["ticket_id"], ["support_tickets.id"]),
        sa.PrimaryKeyConstraint("ticket_id", "person_id"),
        sa.UniqueConstraint("ticket_id", "person_id", name="uq_support_ticket_assignee"),
    )

    op.create_table(
        "support_ticket_comments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ticket_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("author_person_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("is_internal", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("attachments", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["author_person_id"], ["subscribers.id"]),
        sa.ForeignKeyConstraint(["ticket_id"], ["support_tickets.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_support_ticket_comments_ticket", "support_ticket_comments", ["ticket_id"])

    op.create_table(
        "support_ticket_sla_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ticket_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("expected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actual_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["ticket_id"], ["support_tickets.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_support_ticket_sla_events_ticket", "support_ticket_sla_events", ["ticket_id"])

    op.create_table(
        "support_ticket_merges",
        sa.Column("source_ticket_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_ticket_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("merged_by_person_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["merged_by_person_id"], ["subscribers.id"]),
        sa.ForeignKeyConstraint(["source_ticket_id"], ["support_tickets.id"]),
        sa.ForeignKeyConstraint(["target_ticket_id"], ["support_tickets.id"]),
        sa.PrimaryKeyConstraint("source_ticket_id", "target_ticket_id"),
        sa.UniqueConstraint("source_ticket_id", "target_ticket_id", name="uq_support_ticket_merge_pair"),
    )

    op.create_table(
        "support_ticket_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("from_ticket_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("to_ticket_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("link_type", sa.String(length=80), nullable=False),
        sa.Column("created_by_person_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["created_by_person_id"], ["subscribers.id"]),
        sa.ForeignKeyConstraint(["from_ticket_id"], ["support_tickets.id"]),
        sa.ForeignKeyConstraint(["to_ticket_id"], ["support_tickets.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("from_ticket_id", "to_ticket_id", "link_type", name="uq_support_ticket_link"),
    )


def downgrade() -> None:
    op.drop_table("support_ticket_links")
    op.drop_table("support_ticket_merges")
    op.drop_index("ix_support_ticket_sla_events_ticket", table_name="support_ticket_sla_events")
    op.drop_table("support_ticket_sla_events")
    op.drop_index("ix_support_ticket_comments_ticket", table_name="support_ticket_comments")
    op.drop_table("support_ticket_comments")
    op.drop_table("support_ticket_assignees")
    op.drop_index("ix_support_tickets_active", table_name="support_tickets")
    op.drop_index("ix_support_tickets_subscriber", table_name="support_tickets")
    op.drop_index("ix_support_tickets_priority", table_name="support_tickets")
    op.drop_index("ix_support_tickets_status", table_name="support_tickets")
    op.drop_index("ix_support_tickets_number", table_name="support_tickets")
    op.drop_table("support_tickets")

    bind = op.get_bind()
    sa.Enum(name="ticketchannel").drop(bind, checkfirst=True)
    sa.Enum(name="ticketpriority").drop(bind, checkfirst=True)
    sa.Enum(name="ticketstatus").drop(bind, checkfirst=True)
