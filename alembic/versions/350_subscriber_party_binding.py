"""Add the guarded canonical Party binding to subscriber accounts.

One Party may own several service/billing accounts, so ``party_id`` is indexed
but deliberately not unique. Binding provenance is required whenever the link
is populated. This migration is schema-only: it does not infer identities,
assign roles, copy contacts, or backfill any subscriber row.

Revision ID: 350_subscriber_party_binding
Revises: 349_party_role_foundation
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "350_subscriber_party_binding"
down_revision = "349_party_role_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subscribers",
        sa.Column("party_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "subscribers",
        sa.Column("party_bound_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "subscribers",
        sa.Column("party_binding_source", sa.String(length=80), nullable=True),
    )
    op.add_column(
        "subscribers",
        sa.Column("party_binding_reason", sa.Text(), nullable=True),
    )
    op.create_foreign_key(
        "fk_subscribers_party_id",
        "subscribers",
        "parties",
        ["party_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_subscribers_party_binding_evidence",
        "subscribers",
        "(party_id IS NULL AND party_bound_at IS NULL AND "
        "party_binding_source IS NULL AND party_binding_reason IS NULL) OR "
        "(party_id IS NOT NULL AND party_bound_at IS NOT NULL AND "
        "party_binding_source IS NOT NULL AND "
        "party_binding_reason IS NOT NULL AND "
        "length(trim(party_binding_source)) > 0 AND "
        "length(trim(party_binding_reason)) > 0)",
    )
    op.create_index("ix_subscribers_party_id", "subscribers", ["party_id"])


def downgrade() -> None:
    op.drop_index("ix_subscribers_party_id", table_name="subscribers")
    op.drop_constraint(
        "ck_subscribers_party_binding_evidence",
        "subscribers",
        type_="check",
    )
    op.drop_constraint("fk_subscribers_party_id", "subscribers", type_="foreignkey")
    op.drop_column("subscribers", "party_binding_reason")
    op.drop_column("subscribers", "party_binding_source")
    op.drop_column("subscribers", "party_bound_at")
    op.drop_column("subscribers", "party_id")
