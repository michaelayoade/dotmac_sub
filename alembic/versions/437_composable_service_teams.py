"""Expand service teams into composable operational topology.

Revision ID: 437_composable_service_teams
Revises: 436_billing_shadow_verification_evidence
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "437_composable_service_teams"
down_revision = "436_billing_shadow_verification_evidence"
branch_labels = None
depends_on = None


_CAPABILITIES = (
    (
        "operations.general",
        "General operations",
        "General operational coordination that has no narrower capability.",
        "operations.service_team_lifecycle",
    ),
    (
        "support.tickets",
        "Support tickets",
        "Own and process customer support ticket work.",
        "support.ticket_lifecycle",
    ),
    (
        "field_service.work_orders",
        "Field work orders",
        "Execute dispatched field-service work orders.",
        "operations.work_order_commands",
    ),
    (
        "network.outages.coordinate",
        "Coordinate network outages",
        "Act as the explicitly routed primary team for an outage.",
        "network.outage_lifecycle",
    ),
    (
        "network.outages.observe",
        "Observe network outages",
        "Participate in explicitly routed outage coordination.",
        "network.outage_lifecycle",
    ),
    (
        "billing.operations",
        "Billing operations",
        "Perform billing and receivables operations.",
        "billing.operations",
    ),
    (
        "projects.manage",
        "Project management",
        "Coordinate and manage project delivery.",
        "operations.project_lifecycle",
    ),
    (
        "communications.inbox",
        "Team inbox",
        "Own or participate in team inbox conversations.",
        "communications.team_inbox_commands",
    ),
)

_RESPONSIBILITIES = (
    (
        "accountable_manager",
        "Accountable manager",
        "Accountable for the team's operational outcomes.",
        "team",
    ),
    (
        "queue_lead",
        "Queue lead",
        "Coordinates queued work for this team.",
        "workqueue",
    ),
    ("agent", "Agent", "Performs work assigned to this team.", "self"),
    (
        "dispatcher",
        "Dispatcher",
        "Coordinates field-work assignment for this team.",
        "dispatch",
    ),
    ("on_call", "On-call", "Acts as an on-call responder for this team.", "incident"),
)

_TYPE_CAPABILITIES = {
    "operations": ("operations.general", "network.outages.coordinate"),
    "support": (
        "support.tickets",
        "communications.inbox",
        "network.outages.observe",
    ),
    "field_service": (
        "field_service.work_orders",
        "network.outages.observe",
    ),
    "billing": ("billing.operations",),
    "project_management": ("projects.manage",),
}

_ROLE_RESPONSIBILITY = {
    "member": "agent",
    "lead": "queue_lead",
    "manager": "accountable_manager",
}


def _seed_vocabulary(bind) -> None:
    bind.execute(
        sa.text(
            "INSERT INTO service_team_capability_definitions "
            "(key, name, description, contract_owner, is_active, created_at) "
            "VALUES (:key, :name, :description, :contract_owner, TRUE, now())"
        ),
        [
            {
                "key": key,
                "name": name,
                "description": description,
                "contract_owner": owner,
            }
            for key, name, description, owner in _CAPABILITIES
        ],
    )
    bind.execute(
        sa.text(
            "INSERT INTO service_team_responsibility_definitions "
            "(key, name, description, operational_scope, is_active, created_at) "
            "VALUES (:key, :name, :description, :operational_scope, TRUE, now())"
        ),
        [
            {
                "key": key,
                "name": name,
                "description": description,
                "operational_scope": scope,
            }
            for key, name, description, scope in _RESPONSIBILITIES
        ],
    )


def _backfill_shadow_projection(bind) -> None:
    for legacy_type, capability_keys in _TYPE_CAPABILITIES.items():
        for capability_key in capability_keys:
            bind.execute(
                sa.text(
                    "INSERT INTO service_team_capabilities "
                    "(id, team_id, capability_key, is_active, created_at) "
                    "SELECT gen_random_uuid(), id, :capability_key, TRUE, now() "
                    "FROM service_teams WHERE team_type = :legacy_type "
                    "ON CONFLICT (team_id, capability_key) DO NOTHING"
                ),
                {
                    "legacy_type": legacy_type,
                    "capability_key": capability_key,
                },
            )

    for legacy_role, responsibility_key in _ROLE_RESPONSIBILITY.items():
        bind.execute(
            sa.text(
                "INSERT INTO service_team_member_responsibilities "
                "(id, membership_id, responsibility_key, is_active, assigned_at) "
                "SELECT gen_random_uuid(), id, :responsibility_key, is_active, "
                "created_at FROM service_team_members WHERE role = :legacy_role "
                "ON CONFLICT (membership_id, responsibility_key) DO NOTHING"
            ),
            {
                "legacy_role": legacy_role,
                "responsibility_key": responsibility_key,
            },
        )

    bind.execute(
        sa.text(
            "INSERT INTO service_team_external_references "
            "(id, team_id, system, entity_type, external_reference, observed_at, "
            "is_active, created_at) "
            "SELECT gen_random_uuid(), id, workforce_system, 'department', "
            "workforce_department_reference, updated_at, TRUE, now() "
            "FROM service_teams "
            "WHERE workforce_system IS NOT NULL "
            "AND workforce_department_reference IS NOT NULL "
            "ON CONFLICT (system, entity_type, external_reference) DO NOTHING"
        )
    )


def upgrade() -> None:
    op.create_table(
        "service_team_capability_definitions",
        sa.Column("key", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("contract_owner", sa.String(length=120), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "service_team_responsibility_definitions",
        sa.Column("key", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("operational_scope", sa.String(length=80), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "service_team_capabilities",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("team_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("capability_key", sa.String(length=80), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["capability_key"],
            ["service_team_capability_definitions.key"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["team_id"],
            ["service_teams.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "team_id",
            "capability_key",
            name="uq_service_team_capability",
        ),
    )
    op.create_index(
        "ix_service_team_capabilities_lookup",
        "service_team_capabilities",
        ["capability_key", "is_active"],
    )
    op.create_table(
        "service_team_member_responsibilities",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("membership_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("responsibility_key", sa.String(length=80), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["membership_id"],
            ["service_team_members.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["responsibility_key"],
            ["service_team_responsibility_definitions.key"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "membership_id",
            "responsibility_key",
            name="uq_service_team_member_responsibility",
        ),
    )
    op.create_index(
        "ix_service_team_member_responsibilities_lookup",
        "service_team_member_responsibilities",
        ["responsibility_key", "is_active"],
    )
    op.create_table(
        "service_team_relationships",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parent_team_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("child_team_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("relationship_type", sa.String(length=80), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "parent_team_id <> child_team_id",
            name="ck_service_team_relationship_not_self",
        ),
        sa.ForeignKeyConstraint(
            ["child_team_id"],
            ["service_teams.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["parent_team_id"],
            ["service_teams.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "parent_team_id",
            "child_team_id",
            "relationship_type",
            name="uq_service_team_relationship",
        ),
    )
    op.create_table(
        "service_team_scope_bindings",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("team_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scope_type", sa.String(length=40), nullable=False),
        sa.Column("geo_area_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "scope_type = 'geo_area' AND geo_area_id IS NOT NULL",
            name="ck_service_team_scope_binding_typed_target",
        ),
        sa.ForeignKeyConstraint(
            ["geo_area_id"],
            ["geo_areas.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["team_id"],
            ["service_teams.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "team_id",
            "scope_type",
            "geo_area_id",
            name="uq_service_team_scope_binding",
        ),
    )
    op.create_index(
        "ix_service_team_scope_bindings_geo_area",
        "service_team_scope_bindings",
        ["geo_area_id", "is_active"],
    )
    op.create_table(
        "service_team_external_references",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("team_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("system", sa.String(length=80), nullable=False),
        sa.Column("entity_type", sa.String(length=80), nullable=False),
        sa.Column("external_reference", sa.String(length=200), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["team_id"],
            ["service_teams.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "system",
            "entity_type",
            "external_reference",
            name="uq_service_team_external_reference",
        ),
        sa.UniqueConstraint(
            "team_id",
            "system",
            "entity_type",
            name="uq_service_team_external_reference_kind",
        ),
    )
    op.create_index(
        "ix_service_team_external_references_team",
        "service_team_external_references",
        ["team_id", "is_active"],
    )
    op.create_table(
        "outage_team_routing_policies",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("purpose", sa.String(length=40), nullable=False),
        sa.Column("service_team_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("required_capability_key", sa.String(length=80), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["required_capability_key"],
            ["service_team_capability_definitions.key"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["service_team_id"],
            ["service_teams.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "purpose",
            "service_team_id",
            name="uq_outage_team_routing_policy",
        ),
    )
    op.create_index(
        "ix_outage_team_routing_policies_active",
        "outage_team_routing_policies",
        ["purpose", "is_active", "priority"],
    )
    op.create_index(
        "uq_outage_team_routing_primary_active",
        "outage_team_routing_policies",
        ["purpose"],
        unique=True,
        postgresql_where=sa.text("is_active IS TRUE AND purpose = 'primary_owner'"),
        sqlite_where=sa.text("is_active IS TRUE AND purpose = 'primary_owner'"),
    )

    bind = op.get_bind()
    _seed_vocabulary(bind)
    _backfill_shadow_projection(bind)


def downgrade() -> None:
    op.drop_index(
        "uq_outage_team_routing_primary_active",
        table_name="outage_team_routing_policies",
    )
    op.drop_index(
        "ix_outage_team_routing_policies_active",
        table_name="outage_team_routing_policies",
    )
    op.drop_table("outage_team_routing_policies")
    op.drop_index(
        "ix_service_team_external_references_team",
        table_name="service_team_external_references",
    )
    op.drop_table("service_team_external_references")
    op.drop_index(
        "ix_service_team_scope_bindings_geo_area",
        table_name="service_team_scope_bindings",
    )
    op.drop_table("service_team_scope_bindings")
    op.drop_table("service_team_relationships")
    op.drop_index(
        "ix_service_team_member_responsibilities_lookup",
        table_name="service_team_member_responsibilities",
    )
    op.drop_table("service_team_member_responsibilities")
    op.drop_index(
        "ix_service_team_capabilities_lookup",
        table_name="service_team_capabilities",
    )
    op.drop_table("service_team_capabilities")
    op.drop_table("service_team_responsibility_definitions")
    op.drop_table("service_team_capability_definitions")
