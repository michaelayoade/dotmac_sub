"""Add canonical Person and organization-context links to auth projections.

SystemUser and ResellerUser gain reviewed Person Party links. ResellerUser,
OrganizationMembership, and FieldVendorUser gain reviewed links to the
canonical PartyMembership that names both the person and organization context.
The unused native VendorUser remains a compatibility retirement target.

This migration is schema-only. It does not infer an identity or context, create
or activate a membership, assign a role or permission, alter credentials, or
change any login/read path.

Revision ID: 353_party_principal_context_bindings
Revises: 352_party_organization_profile_bindings
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "353_party_principal_context_bindings"
down_revision = "352_party_organization_profile_bindings"
branch_labels = None
depends_on = None

_EVIDENCE_COLUMNS = (
    "party_bound_at",
    "party_binding_source",
    "party_binding_reason",
)


def _add_evidence_columns(table_name: str) -> None:
    op.add_column(
        table_name,
        sa.Column("party_bound_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        table_name,
        sa.Column("party_binding_source", sa.String(length=80), nullable=True),
    )
    op.add_column(
        table_name,
        sa.Column("party_binding_reason", sa.Text(), nullable=True),
    )


def _evidence_constraint(*binding_columns: str) -> str:
    unbound = " AND ".join(f"{column} IS NULL" for column in binding_columns)
    bound = " AND ".join(f"{column} IS NOT NULL" for column in binding_columns)
    return (
        f"({unbound} AND party_bound_at IS NULL AND "
        "party_binding_source IS NULL AND party_binding_reason IS NULL) OR "
        f"({bound} AND party_bound_at IS NOT NULL AND "
        "party_binding_source IS NOT NULL AND party_binding_reason IS NOT NULL AND "
        "length(trim(party_binding_source)) > 0 AND "
        "length(trim(party_binding_reason)) > 0)"
    )


def _add_membership_binding(table_name: str) -> None:
    op.add_column(
        table_name,
        sa.Column("party_membership_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    _add_evidence_columns(table_name)
    op.create_foreign_key(
        f"fk_{table_name}_party_membership_id",
        table_name,
        "party_memberships",
        ["party_membership_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        f"ck_{table_name}_party_binding_evidence",
        table_name,
        _evidence_constraint("party_membership_id"),
    )
    op.create_unique_constraint(
        f"uq_{table_name}_party_membership_id",
        table_name,
        ["party_membership_id"],
    )


def upgrade() -> None:
    op.add_column(
        "system_users",
        sa.Column("person_party_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    _add_evidence_columns("system_users")
    op.create_foreign_key(
        "fk_system_users_person_party_id",
        "system_users",
        "parties",
        ["person_party_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_system_users_party_binding_evidence",
        "system_users",
        _evidence_constraint("person_party_id"),
    )
    op.create_unique_constraint(
        "uq_system_users_person_party_id",
        "system_users",
        ["person_party_id"],
    )

    op.add_column(
        "reseller_users",
        sa.Column("person_party_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "reseller_users",
        sa.Column("party_membership_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    _add_evidence_columns("reseller_users")
    op.create_foreign_key(
        "fk_reseller_users_person_party_id",
        "reseller_users",
        "parties",
        ["person_party_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_reseller_users_party_membership_id",
        "reseller_users",
        "party_memberships",
        ["party_membership_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_reseller_users_party_binding_evidence",
        "reseller_users",
        _evidence_constraint("person_party_id", "party_membership_id"),
    )
    op.create_unique_constraint(
        "uq_reseller_users_reseller_person_party",
        "reseller_users",
        ["reseller_id", "person_party_id"],
    )
    op.create_unique_constraint(
        "uq_reseller_users_party_membership_id",
        "reseller_users",
        ["party_membership_id"],
    )

    for table_name in (
        "organization_memberships",
        "field_vendor_users",
    ):
        _add_membership_binding(table_name)


def _drop_membership_binding(table_name: str) -> None:
    op.drop_constraint(
        f"uq_{table_name}_party_membership_id",
        table_name,
        type_="unique",
    )
    op.drop_constraint(
        f"ck_{table_name}_party_binding_evidence",
        table_name,
        type_="check",
    )
    op.drop_constraint(
        f"fk_{table_name}_party_membership_id",
        table_name,
        type_="foreignkey",
    )
    for column_name in reversed(_EVIDENCE_COLUMNS):
        op.drop_column(table_name, column_name)
    op.drop_column(table_name, "party_membership_id")


def downgrade() -> None:
    for table_name in (
        "field_vendor_users",
        "organization_memberships",
    ):
        _drop_membership_binding(table_name)

    op.drop_constraint(
        "uq_reseller_users_party_membership_id",
        "reseller_users",
        type_="unique",
    )
    op.drop_constraint(
        "uq_reseller_users_reseller_person_party",
        "reseller_users",
        type_="unique",
    )
    op.drop_constraint(
        "ck_reseller_users_party_binding_evidence",
        "reseller_users",
        type_="check",
    )
    op.drop_constraint(
        "fk_reseller_users_party_membership_id",
        "reseller_users",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_reseller_users_person_party_id",
        "reseller_users",
        type_="foreignkey",
    )
    for column_name in reversed(_EVIDENCE_COLUMNS):
        op.drop_column("reseller_users", column_name)
    op.drop_column("reseller_users", "party_membership_id")
    op.drop_column("reseller_users", "person_party_id")

    op.drop_constraint(
        "uq_system_users_person_party_id",
        "system_users",
        type_="unique",
    )
    op.drop_constraint(
        "ck_system_users_party_binding_evidence",
        "system_users",
        type_="check",
    )
    op.drop_constraint(
        "fk_system_users_person_party_id",
        "system_users",
        type_="foreignkey",
    )
    for column_name in reversed(_EVIDENCE_COLUMNS):
        op.drop_column("system_users", column_name)
    op.drop_column("system_users", "person_party_id")
