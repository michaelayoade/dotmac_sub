"""Support tickets Phase 1 expand: merged-model columns, staff-FK drops,
access tokens, assignment/SLA engine tables, service teams.

Revision ID: 221_support_tickets_phase1_expand
Revises: 220_add_network_weathermap_views
Create Date: 2026-07-08
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "221_support_tickets_phase1_expand"
down_revision = "220_add_network_weathermap_views"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(name: str) -> bool:
    return _inspector().has_table(name)


def _has_column(table_name: str, column_name: str) -> bool:
    return any(c["name"] == column_name for c in _inspector().get_columns(table_name))


def _has_index(table_name: str, index_name: str) -> bool:
    return any(i["name"] == index_name for i in _inspector().get_indexes(table_name))


def _drop_fk_if_present(table_name: str, constrained_columns: list[str]) -> None:
    for foreign_key in _inspector().get_foreign_keys(table_name):
        if foreign_key.get("constrained_columns") == constrained_columns:
            op.drop_constraint(
                foreign_key["name"],
                table_name,
                type_="foreignkey",
            )
            return


def upgrade() -> None:
    op.alter_column(
        "support_tickets",
        "ticket_type",
        existing_type=sa.String(length=80),
        type_=sa.String(length=120),
        existing_nullable=True,
    )

    if not _has_column("support_tickets", "erpnext_id"):
        op.add_column(
            "support_tickets", sa.Column("erpnext_id", sa.String(length=100))
        )
    if not _has_index("support_tickets", "ix_support_tickets_erpnext_id"):
        op.create_index(
            "ix_support_tickets_erpnext_id",
            "support_tickets",
            ["erpnext_id"],
            unique=True,
            postgresql_where=sa.text("erpnext_id IS NOT NULL"),
        )
    if not _has_index("support_tickets", "ix_support_tickets_region"):
        op.create_index(
            "ix_support_tickets_region",
            "support_tickets",
            ["region"],
            postgresql_where=sa.text("is_active"),
        )
    if not _has_index("support_tickets", "ix_support_tickets_service_team"):
        op.create_index(
            "ix_support_tickets_service_team",
            "support_tickets",
            ["service_team_id"],
            postgresql_where=sa.text("is_active"),
        )

    _drop_fk_if_present("support_tickets", ["created_by_person_id"])
    _drop_fk_if_present("support_ticket_merges", ["merged_by_person_id"])
    _drop_fk_if_present("support_ticket_links", ["created_by_person_id"])

    if not _has_table("service_teams"):
        op.create_table(
            "service_teams",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("name", sa.String(length=160), nullable=False),
            sa.Column("team_type", sa.String(length=40), nullable=False),
            sa.Column("region", sa.String(length=80)),
            sa.Column("manager_person_id", postgresql.UUID(as_uuid=True)),
            sa.Column("erp_department", sa.String(length=120)),
            sa.Column(
                "is_active", sa.Boolean(), nullable=False, server_default=sa.true()
            ),
            sa.Column("metadata", sa.JSON()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "erp_department", name="uq_service_teams_erp_department"
            ),
        )

    if not _has_table("service_team_members"):
        op.create_table(
            "service_team_members",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("team_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("person_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column(
                "role",
                sa.String(length=40),
                nullable=False,
                server_default="member",
            ),
            sa.Column(
                "is_active", sa.Boolean(), nullable=False, server_default=sa.true()
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["team_id"], ["service_teams.id"]),
            sa.UniqueConstraint(
                "team_id", "person_id", name="uq_service_team_member"
            ),
        )
        op.create_index(
            "ix_service_team_members_person_id",
            "service_team_members",
            ["person_id"],
        )

    if not _has_table("sla_policies"):
        op.create_table(
            "sla_policies",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("name", sa.String(length=160), nullable=False),
            sa.Column("entity_type", sa.String(length=40), nullable=False),
            sa.Column("description", sa.Text()),
            sa.Column(
                "is_active", sa.Boolean(), nullable=False, server_default=sa.true()
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )

    if not _has_table("sla_targets"):
        op.create_table(
            "sla_targets",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("policy_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("priority", sa.String(length=40)),
            sa.Column("target_minutes", sa.Integer(), nullable=False),
            sa.Column("warning_minutes", sa.Integer()),
            sa.Column(
                "is_active", sa.Boolean(), nullable=False, server_default=sa.true()
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["policy_id"], ["sla_policies.id"]),
            sa.UniqueConstraint(
                "policy_id", "priority", name="uq_sla_targets_policy_priority"
            ),
        )

    if not _has_table("sla_clocks"):
        op.create_table(
            "sla_clocks",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("policy_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("entity_type", sa.String(length=40), nullable=False),
            sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("priority", sa.String(length=40)),
            sa.Column(
                "status",
                sa.String(length=40),
                nullable=False,
                server_default="running",
            ),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("paused_at", sa.DateTime(timezone=True)),
            sa.Column(
                "total_paused_seconds",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
            sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True)),
            sa.Column("breached_at", sa.DateTime(timezone=True)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["policy_id"], ["sla_policies.id"]),
        )

    if not _has_table("sla_breaches"):
        op.create_table(
            "sla_breaches",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("clock_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column(
                "status", sa.String(length=40), nullable=False, server_default="open"
            ),
            sa.Column("breached_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("notes", sa.Text()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["clock_id"], ["sla_clocks.id"]),
        )

    if not _has_table("ticket_assignment_rules"):
        op.create_table(
            "ticket_assignment_rules",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("name", sa.String(length=160), nullable=False),
            sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "is_active", sa.Boolean(), nullable=False, server_default=sa.true()
            ),
            sa.Column("match_config", sa.JSON()),
            sa.Column(
                "strategy",
                sa.String(length=40),
                nullable=False,
                server_default="round_robin",
            ),
            sa.Column("team_id", postgresql.UUID(as_uuid=True)),
            sa.Column(
                "assign_manager",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
            sa.Column(
                "assign_spc", sa.Boolean(), nullable=False, server_default=sa.false()
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["team_id"], ["service_teams.id"]),
        )

    if not _has_table("ticket_assignment_counters"):
        op.create_table(
            "ticket_assignment_counters",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("rule_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("last_assigned_person_id", postgresql.UUID(as_uuid=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["rule_id"], ["ticket_assignment_rules.id"]),
            sa.UniqueConstraint(
                "rule_id", name="uq_ticket_assignment_counters_rule_id"
            ),
        )

    if not _has_table("ticket_access_tokens"):
        op.create_table(
            "ticket_access_tokens",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("ticket_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("token", sa.String(length=64), nullable=False),
            sa.Column(
                "purpose",
                sa.String(length=40),
                nullable=False,
                server_default="resolution_confirm",
            ),
            sa.Column("expires_at", sa.DateTime(timezone=True)),
            sa.Column("accessed_at", sa.DateTime(timezone=True)),
            sa.Column("responded_at", sa.DateTime(timezone=True)),
            sa.Column(
                "is_active", sa.Boolean(), nullable=False, server_default=sa.true()
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["ticket_id"], ["support_tickets.id"]),
        )
        op.create_index(
            "ix_ticket_access_tokens_token",
            "ticket_access_tokens",
            ["token"],
            unique=True,
        )
        op.create_index(
            "ix_ticket_access_tokens_ticket_id",
            "ticket_access_tokens",
            ["ticket_id"],
        )


def downgrade() -> None:
    if _has_table("ticket_access_tokens"):
        op.drop_index(
            "ix_ticket_access_tokens_ticket_id", table_name="ticket_access_tokens"
        )
        op.drop_index(
            "ix_ticket_access_tokens_token", table_name="ticket_access_tokens"
        )
        op.drop_table("ticket_access_tokens")
    if _has_table("ticket_assignment_counters"):
        op.drop_table("ticket_assignment_counters")
    if _has_table("ticket_assignment_rules"):
        op.drop_table("ticket_assignment_rules")
    if _has_table("sla_breaches"):
        op.drop_table("sla_breaches")
    if _has_table("sla_clocks"):
        op.drop_table("sla_clocks")
    if _has_table("sla_targets"):
        op.drop_table("sla_targets")
    if _has_table("sla_policies"):
        op.drop_table("sla_policies")
    if _has_table("service_team_members"):
        op.drop_table("service_team_members")
    if _has_table("service_teams"):
        op.drop_table("service_teams")

    op.create_foreign_key(
        "fk_support_ticket_links_created_by_person_id_subscribers",
        "support_ticket_links",
        "subscribers",
        ["created_by_person_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_support_ticket_merges_merged_by_person_id_subscribers",
        "support_ticket_merges",
        "subscribers",
        ["merged_by_person_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_support_tickets_created_by_person_id_subscribers",
        "support_tickets",
        "subscribers",
        ["created_by_person_id"],
        ["id"],
    )

    if _has_index("support_tickets", "ix_support_tickets_service_team"):
        op.drop_index(
            "ix_support_tickets_service_team", table_name="support_tickets"
        )
    if _has_index("support_tickets", "ix_support_tickets_region"):
        op.drop_index("ix_support_tickets_region", table_name="support_tickets")
    if _has_index("support_tickets", "ix_support_tickets_erpnext_id"):
        op.drop_index(
            "ix_support_tickets_erpnext_id", table_name="support_tickets"
        )
    if _has_column("support_tickets", "erpnext_id"):
        op.drop_column("support_tickets", "erpnext_id")
    op.alter_column(
        "support_tickets",
        "ticket_type",
        existing_type=sa.String(length=120),
        type_=sa.String(length=80),
        existing_nullable=True,
    )
