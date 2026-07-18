"""Add reviewed contact identity and Inbox routing projections.

SubscriberContact gains a nullable, evidence-bound Person Party link. Two
projection tables map reviewed legacy relationship facts and individual source
fields to existing canonical PartyRelationship and PartyContactPoint rows.
InboxContactLink gains a nullable, evidence-bound PartyContactPoint projection.

This migration is schema-only. It does not infer a person from contact values,
create a Party/relationship/contact point, copy verification or consent, change
contact authorization flags, resolve a conversation, or change Inbox routing.

Revision ID: 354_party_contact_inbox_projections
Revises: 353_party_principal_context_bindings
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "354_party_contact_inbox_projections"
down_revision = "353_party_principal_context_bindings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subscriber_contacts",
        sa.Column("person_party_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "subscriber_contacts",
        sa.Column("party_bound_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "subscriber_contacts",
        sa.Column("party_binding_source", sa.String(length=80), nullable=True),
    )
    op.add_column(
        "subscriber_contacts",
        sa.Column("party_binding_reason", sa.Text(), nullable=True),
    )
    op.create_foreign_key(
        "fk_subscriber_contacts_person_party_id",
        "subscriber_contacts",
        "parties",
        ["person_party_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_subscriber_contacts_party_binding_evidence",
        "subscriber_contacts",
        "(person_party_id IS NULL AND party_bound_at IS NULL AND "
        "party_binding_source IS NULL AND party_binding_reason IS NULL) OR "
        "(person_party_id IS NOT NULL AND party_bound_at IS NOT NULL AND "
        "party_binding_source IS NOT NULL AND "
        "party_binding_reason IS NOT NULL AND "
        "length(trim(party_binding_source)) > 0 AND "
        "length(trim(party_binding_reason)) > 0)",
    )
    op.create_unique_constraint(
        "uq_subscriber_contacts_subscriber_person_party",
        "subscriber_contacts",
        ["subscriber_id", "person_party_id"],
    )

    op.create_table(
        "subscriber_contact_relationship_projections",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "subscriber_contact_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "party_relationship_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("bound_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("binding_source", sa.String(length=80), nullable=False),
        sa.Column("binding_reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(trim(binding_source)) > 0 AND length(trim(binding_reason)) > 0",
            name="ck_subscriber_contact_relationship_projection_evidence",
        ),
        sa.ForeignKeyConstraint(
            ["subscriber_contact_id"],
            ["subscriber_contacts.id"],
            name="fk_subscriber_contact_relationship_projection_contact",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["party_relationship_id"],
            ["party_relationships.id"],
            name="fk_subscriber_contact_relationship_projection_relationship",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "id", name="pk_subscriber_contact_relationship_projections"
        ),
        sa.UniqueConstraint(
            "subscriber_contact_id",
            "party_relationship_id",
            name="uq_subscriber_contact_relationship_projection",
        ),
        sa.UniqueConstraint(
            "party_relationship_id",
            name="uq_subscriber_contact_relationship_party_relationship",
        ),
    )
    op.create_index(
        "ix_subscriber_contact_relationship_projection_contact",
        "subscriber_contact_relationship_projections",
        ["subscriber_contact_id"],
    )

    op.create_table(
        "subscriber_contact_point_projections",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "subscriber_contact_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("source_field", sa.String(length=32), nullable=False),
        sa.Column(
            "party_contact_point_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("bound_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("binding_source", sa.String(length=80), nullable=False),
        sa.Column("binding_reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source_field IN ('email', 'phone', 'whatsapp', 'facebook', "
            "'instagram', 'x_handle', 'telegram', 'linkedin')",
            name="ck_subscriber_contact_point_projection_source_field",
        ),
        sa.CheckConstraint(
            "length(trim(binding_source)) > 0 AND length(trim(binding_reason)) > 0",
            name="ck_subscriber_contact_point_projection_evidence",
        ),
        sa.ForeignKeyConstraint(
            ["subscriber_contact_id"],
            ["subscriber_contacts.id"],
            name="fk_subscriber_contact_point_projection_contact",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["party_contact_point_id"],
            ["party_contact_points.id"],
            name="fk_subscriber_contact_point_projection_point",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_subscriber_contact_point_projections"),
        sa.UniqueConstraint(
            "subscriber_contact_id",
            "source_field",
            name="uq_subscriber_contact_point_projection_source",
        ),
        sa.UniqueConstraint(
            "subscriber_contact_id",
            "party_contact_point_id",
            name="uq_subscriber_contact_point_projection_point",
        ),
    )
    op.create_index(
        "ix_subscriber_contact_point_projection_contact",
        "subscriber_contact_point_projections",
        ["subscriber_contact_id"],
    )
    op.create_index(
        "ix_subscriber_contact_point_projection_point",
        "subscriber_contact_point_projections",
        ["party_contact_point_id"],
    )

    op.add_column(
        "inbox_contact_links",
        sa.Column(
            "party_contact_point_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
    )
    op.add_column(
        "inbox_contact_links",
        sa.Column(
            "party_contact_point_bound_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "inbox_contact_links",
        sa.Column(
            "party_contact_point_binding_source",
            sa.String(length=80),
            nullable=True,
        ),
    )
    op.add_column(
        "inbox_contact_links",
        sa.Column("party_contact_point_binding_reason", sa.Text(), nullable=True),
    )
    op.create_foreign_key(
        "fk_inbox_contact_links_party_contact_point_id",
        "inbox_contact_links",
        "party_contact_points",
        ["party_contact_point_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_inbox_contact_links_party_contact_point_evidence",
        "inbox_contact_links",
        "(party_contact_point_id IS NULL AND "
        "party_contact_point_bound_at IS NULL AND "
        "party_contact_point_binding_source IS NULL AND "
        "party_contact_point_binding_reason IS NULL) OR "
        "(party_contact_point_id IS NOT NULL AND "
        "party_contact_point_bound_at IS NOT NULL AND "
        "party_contact_point_binding_source IS NOT NULL AND "
        "party_contact_point_binding_reason IS NOT NULL AND "
        "length(trim(party_contact_point_binding_source)) > 0 AND "
        "length(trim(party_contact_point_binding_reason)) > 0)",
    )
    op.create_index(
        "ix_inbox_contact_links_party_contact_point",
        "inbox_contact_links",
        ["party_contact_point_id", "is_active"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_inbox_contact_links_party_contact_point",
        table_name="inbox_contact_links",
    )
    op.drop_constraint(
        "ck_inbox_contact_links_party_contact_point_evidence",
        "inbox_contact_links",
        type_="check",
    )
    op.drop_constraint(
        "fk_inbox_contact_links_party_contact_point_id",
        "inbox_contact_links",
        type_="foreignkey",
    )
    op.drop_column("inbox_contact_links", "party_contact_point_binding_reason")
    op.drop_column("inbox_contact_links", "party_contact_point_binding_source")
    op.drop_column("inbox_contact_links", "party_contact_point_bound_at")
    op.drop_column("inbox_contact_links", "party_contact_point_id")

    op.drop_index(
        "ix_subscriber_contact_point_projection_point",
        table_name="subscriber_contact_point_projections",
    )
    op.drop_index(
        "ix_subscriber_contact_point_projection_contact",
        table_name="subscriber_contact_point_projections",
    )
    op.drop_table("subscriber_contact_point_projections")

    op.drop_index(
        "ix_subscriber_contact_relationship_projection_contact",
        table_name="subscriber_contact_relationship_projections",
    )
    op.drop_table("subscriber_contact_relationship_projections")

    op.drop_constraint(
        "uq_subscriber_contacts_subscriber_person_party",
        "subscriber_contacts",
        type_="unique",
    )
    op.drop_constraint(
        "ck_subscriber_contacts_party_binding_evidence",
        "subscriber_contacts",
        type_="check",
    )
    op.drop_constraint(
        "fk_subscriber_contacts_person_party_id",
        "subscriber_contacts",
        type_="foreignkey",
    )
    op.drop_column("subscriber_contacts", "party_binding_reason")
    op.drop_column("subscriber_contacts", "party_binding_source")
    op.drop_column("subscriber_contacts", "party_bound_at")
    op.drop_column("subscriber_contacts", "person_party_id")
