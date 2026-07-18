"""Add the canonical party, role, relationship, and contact foundation.

This migration is intentionally additive. Existing subscriber, organization,
reseller, vendor, field-vendor, and authentication tables are not rewired in
this slice. Later migrations backfill native party links one domain at a time.

Revision ID: 349_party_role_foundation
Revises: 345_fiber_topology_field_observations
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "349_party_role_foundation"
down_revision = "348_location_capture_prompt_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "parties",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("party_type", sa.String(length=24), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column(
            "status", sa.String(length=24), server_default="active", nullable=False
        ),
        sa.Column(
            "data_classification",
            sa.String(length=32),
            server_default="production",
            nullable=False,
        ),
        sa.Column("merged_into_party_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("merge_reason", sa.Text(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "party_type IN ('person', 'organization')",
            name="ck_parties_party_type",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'quarantined', 'merged', 'archived')",
            name="ck_parties_status",
        ),
        sa.CheckConstraint(
            "data_classification IN ('production', 'test', 'imported_unverified')",
            name="ck_parties_data_classification",
        ),
        sa.CheckConstraint(
            "merged_into_party_id IS NULL OR merged_into_party_id <> id",
            name="ck_parties_not_merged_into_self",
        ),
        sa.CheckConstraint(
            "(status = 'merged' AND merged_into_party_id IS NOT NULL) OR "
            "(status <> 'merged' AND merged_into_party_id IS NULL)",
            name="ck_parties_merged_target_required",
        ),
        sa.ForeignKeyConstraint(
            ["merged_into_party_id"],
            ["parties.id"],
            name="fk_parties_merged_into_party_id",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_parties"),
    )
    op.create_index("ix_parties_type_status", "parties", ["party_type", "status"])
    op.create_index(
        "ix_parties_classification",
        "parties",
        ["data_classification", "status"],
    )
    op.create_index("ix_parties_merged_into", "parties", ["merged_into_party_id"])

    op.create_table(
        "party_roles",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("party_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role_type", sa.String(length=32), nullable=False),
        sa.Column(
            "role_key", sa.String(length=40), server_default="default", nullable=False
        ),
        sa.Column(
            "status", sa.String(length=24), server_default="pending", nullable=False
        ),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.String(length=80), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role_type IN ('prospect', 'customer', 'subscriber', 'reseller', "
            "'vendor', 'partner', 'staff', 'agent')",
            name="ck_party_roles_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'suspended', 'ended')",
            name="ck_party_roles_status",
        ),
        sa.CheckConstraint(
            "(role_type = 'partner' AND role_key IN "
            "('referral', 'technology', 'infrastructure', 'strategic')) OR "
            "(role_type <> 'partner' AND role_key = 'default')",
            name="ck_party_roles_key_contract",
        ),
        sa.CheckConstraint(
            "valid_until IS NULL OR valid_from IS NULL OR valid_until > valid_from",
            name="ck_party_roles_valid_window",
        ),
        sa.ForeignKeyConstraint(
            ["party_id"], ["parties.id"], name="fk_party_roles_party_id"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_party_roles"),
        sa.UniqueConstraint(
            "party_id",
            "role_type",
            "role_key",
            name="uq_party_roles_party_type_key",
        ),
    )
    op.create_index(
        "ix_party_roles_type_status", "party_roles", ["role_type", "status"]
    )
    op.create_index(
        "ix_party_roles_party_status", "party_roles", ["party_id", "status"]
    )

    op.create_table(
        "party_relationships",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subject_party_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("object_party_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("relationship_type", sa.String(length=48), nullable=False),
        sa.Column(
            "relationship_key",
            sa.String(length=80),
            server_default="default",
            nullable=False,
        ),
        sa.Column(
            "status", sa.String(length=24), server_default="active", nullable=False
        ),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.String(length=80), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "subject_party_id <> object_party_id",
            name="ck_party_relationships_not_self",
        ),
        sa.CheckConstraint(
            "relationship_type IN ('contact_for', 'billing_contact_for', "
            "'technical_contact_for', 'emergency_contact_for', 'employee_of', "
            "'owner_of', 'director_of', 'agent_for', 'account_manager_for', "
            "'referred_by', 'parent_of', 'manages')",
            name="ck_party_relationships_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'inactive', 'ended')",
            name="ck_party_relationships_status",
        ),
        sa.CheckConstraint(
            "valid_until IS NULL OR valid_from IS NULL OR valid_until > valid_from",
            name="ck_party_relationships_valid_window",
        ),
        sa.ForeignKeyConstraint(
            ["subject_party_id"],
            ["parties.id"],
            name="fk_party_relationships_subject_party_id",
        ),
        sa.ForeignKeyConstraint(
            ["object_party_id"],
            ["parties.id"],
            name="fk_party_relationships_object_party_id",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_party_relationships"),
        sa.UniqueConstraint(
            "subject_party_id",
            "object_party_id",
            "relationship_type",
            "relationship_key",
            name="uq_party_relationships_subject_object_type_key",
        ),
    )
    op.create_index(
        "ix_party_relationships_subject",
        "party_relationships",
        ["subject_party_id", "relationship_type", "status"],
    )
    op.create_index(
        "ix_party_relationships_object",
        "party_relationships",
        ["object_party_id", "relationship_type", "status"],
    )

    op.create_table(
        "party_memberships",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("person_party_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "organization_party_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("membership_type", sa.String(length=40), nullable=False),
        sa.Column(
            "membership_key",
            sa.String(length=80),
            server_default="default",
            nullable=False,
        ),
        sa.Column(
            "status", sa.String(length=24), server_default="invited", nullable=False
        ),
        sa.Column("access_scope", sa.JSON(), nullable=True),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.String(length=80), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "person_party_id <> organization_party_id",
            name="ck_party_memberships_not_self",
        ),
        sa.CheckConstraint(
            "membership_type IN ('owner', 'admin', 'member', 'employee', 'agent', "
            "'reseller_admin', 'vendor_user')",
            name="ck_party_memberships_type",
        ),
        sa.CheckConstraint(
            "status IN ('invited', 'active', 'suspended', 'ended')",
            name="ck_party_memberships_status",
        ),
        sa.CheckConstraint(
            "valid_until IS NULL OR valid_from IS NULL OR valid_until > valid_from",
            name="ck_party_memberships_valid_window",
        ),
        sa.ForeignKeyConstraint(
            ["person_party_id"],
            ["parties.id"],
            name="fk_party_memberships_person_party_id",
        ),
        sa.ForeignKeyConstraint(
            ["organization_party_id"],
            ["parties.id"],
            name="fk_party_memberships_organization_party_id",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_party_memberships"),
        sa.UniqueConstraint(
            "person_party_id",
            "organization_party_id",
            "membership_type",
            "membership_key",
            name="uq_party_memberships_person_org_type_key",
        ),
    )
    op.create_index(
        "ix_party_memberships_person",
        "party_memberships",
        ["person_party_id", "status"],
    )
    op.create_index(
        "ix_party_memberships_organization",
        "party_memberships",
        ["organization_party_id", "status"],
    )

    op.create_table(
        "party_contact_points",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("party_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel_type", sa.String(length=40), nullable=False),
        sa.Column("normalized_value", sa.String(length=320), nullable=False),
        sa.Column("display_value", sa.String(length=320), nullable=True),
        sa.Column(
            "scope_key",
            sa.String(length=200),
            server_default="default",
            nullable=False,
        ),
        sa.Column("provider", sa.String(length=80), nullable=True),
        sa.Column("provider_account_id", sa.String(length=200), nullable=True),
        sa.Column("external_subject_id", sa.String(length=200), nullable=True),
        sa.Column(
            "is_primary", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "verification_status",
            sa.String(length=24),
            server_default="unverified",
            nullable=False,
        ),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verification_source", sa.String(length=80), nullable=True),
        sa.Column(
            "consent_status",
            sa.String(length=24),
            server_default="unknown",
            nullable=False,
        ),
        sa.Column("consent_captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "channel_type IN ('email', 'phone', 'sms', 'whatsapp', "
            "'facebook_messenger', 'instagram_dm', 'telegram', 'linkedin', 'x')",
            name="ck_party_contact_points_channel_type",
        ),
        sa.CheckConstraint(
            "verification_status IN ('unverified', 'pending', 'verified', 'failed')",
            name="ck_party_contact_points_verification",
        ),
        sa.CheckConstraint(
            "consent_status IN ('unknown', 'opted_in', 'opted_out', 'not_applicable')",
            name="ck_party_contact_points_consent",
        ),
        sa.ForeignKeyConstraint(
            ["party_id"], ["parties.id"], name="fk_party_contact_points_party_id"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_party_contact_points"),
        sa.UniqueConstraint(
            "party_id",
            "channel_type",
            "normalized_value",
            "scope_key",
            name="uq_party_contact_points_party_channel_value_scope",
        ),
    )
    op.create_index(
        "ix_party_contact_points_lookup",
        "party_contact_points",
        ["channel_type", "normalized_value", "is_active"],
    )
    op.create_index(
        "uq_party_contact_points_primary",
        "party_contact_points",
        ["party_id", "channel_type", "scope_key"],
        unique=True,
        postgresql_where=sa.text("is_primary IS TRUE AND is_active IS TRUE"),
        sqlite_where=sa.text("is_primary IS TRUE AND is_active IS TRUE"),
    )

    op.create_table(
        "party_external_references",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("party_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_system", sa.String(length=80), nullable=False),
        sa.Column("entity_type", sa.String(length=80), nullable=False),
        sa.Column("external_id", sa.String(length=200), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["party_id"],
            ["parties.id"],
            name="fk_party_external_references_party_id",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_party_external_references"),
        sa.UniqueConstraint(
            "source_system",
            "entity_type",
            "external_id",
            name="uq_party_external_refs_source_entity_external",
        ),
        sa.UniqueConstraint(
            "party_id",
            "source_system",
            "entity_type",
            name="uq_party_external_refs_party_source_entity",
        ),
    )
    op.create_index(
        "ix_party_external_refs_party", "party_external_references", ["party_id"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_party_external_refs_party", table_name="party_external_references"
    )
    op.drop_table("party_external_references")

    op.drop_index("uq_party_contact_points_primary", table_name="party_contact_points")
    op.drop_index("ix_party_contact_points_lookup", table_name="party_contact_points")
    op.drop_table("party_contact_points")

    op.drop_index("ix_party_memberships_organization", table_name="party_memberships")
    op.drop_index("ix_party_memberships_person", table_name="party_memberships")
    op.drop_table("party_memberships")

    op.drop_index("ix_party_relationships_object", table_name="party_relationships")
    op.drop_index("ix_party_relationships_subject", table_name="party_relationships")
    op.drop_table("party_relationships")

    op.drop_index("ix_party_roles_party_status", table_name="party_roles")
    op.drop_index("ix_party_roles_type_status", table_name="party_roles")
    op.drop_table("party_roles")

    op.drop_index("ix_parties_merged_into", table_name="parties")
    op.drop_index("ix_parties_classification", table_name="parties")
    op.drop_index("ix_parties_type_status", table_name="parties")
    op.drop_table("parties")
