"""Add canonical Party bindings to organization role profiles.

Organization, Reseller, Vendor, and the FieldVendor auth projection may point
to the same Organization Party. Every link carries immutable review provenance
and is one-to-one within its profile type. This migration is schema-only: it
does not infer a Party, assign a role, repair vendor twins, or cut over reads.

Revision ID: 352_party_organization_profile_bindings
Revises: 351_party_identity_backfill_receipts
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "352_party_organization_profile_bindings"
down_revision = "351_party_identity_backfill_receipts"
branch_labels = None
depends_on = None

_PROFILE_TABLES = (
    "organizations",
    "resellers",
    "vendors",
    "field_vendors",
)


def _add_party_binding(table_name: str) -> None:
    op.add_column(
        table_name,
        sa.Column("party_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
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
    op.create_foreign_key(
        f"fk_{table_name}_party_id",
        table_name,
        "parties",
        ["party_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        f"ck_{table_name}_party_binding_evidence",
        table_name,
        "(party_id IS NULL AND party_bound_at IS NULL AND "
        "party_binding_source IS NULL AND party_binding_reason IS NULL) OR "
        "(party_id IS NOT NULL AND party_bound_at IS NOT NULL AND "
        "party_binding_source IS NOT NULL AND "
        "party_binding_reason IS NOT NULL AND "
        "length(trim(party_binding_source)) > 0 AND "
        "length(trim(party_binding_reason)) > 0)",
    )
    op.create_unique_constraint(
        f"uq_{table_name}_party_id",
        table_name,
        ["party_id"],
    )


def upgrade() -> None:
    for table_name in _PROFILE_TABLES:
        _add_party_binding(table_name)


def _drop_party_binding(table_name: str) -> None:
    op.drop_constraint(
        f"uq_{table_name}_party_id",
        table_name,
        type_="unique",
    )
    op.drop_constraint(
        f"ck_{table_name}_party_binding_evidence",
        table_name,
        type_="check",
    )
    op.drop_constraint(
        f"fk_{table_name}_party_id",
        table_name,
        type_="foreignkey",
    )
    op.drop_column(table_name, "party_binding_reason")
    op.drop_column(table_name, "party_binding_source")
    op.drop_column(table_name, "party_bound_at")
    op.drop_column(table_name, "party_id")


def downgrade() -> None:
    for table_name in reversed(_PROFILE_TABLES):
        _drop_party_binding(table_name)
