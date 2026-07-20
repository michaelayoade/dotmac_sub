"""Bind referrals to Party before reviewed Subscriber conversion.

New referral capture can identify a quarantined Party and an attributed Lead
without creating a fake Subscriber account. Optional Subscriber attachment is
evidence-bound and remains a later exact-Party command. Legacy referral rows
remain valid and are not inferred, rewritten, or classified by this migration.

This migration is schema-only. It does not create Parties, contact points,
Leads, Subscribers, roles, rewards, or relationships; copy contact PII out of
legacy metadata; attach accounts; qualify referrals; or change billing,
subscription, access, or referral status.

Revision ID: 356_party_first_referral_capture
Revises: 355_party_customer_lifecycle
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "356_party_first_referral_capture"
down_revision = "355_party_customer_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "referrals",
        sa.Column("referred_party_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "referrals",
        sa.Column("party_bound_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "referrals",
        sa.Column("party_binding_source", sa.String(length=80), nullable=True),
    )
    op.add_column(
        "referrals", sa.Column("party_binding_reason", sa.Text(), nullable=True)
    )
    op.add_column(
        "referrals",
        sa.Column("subscriber_linked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "referrals",
        sa.Column("subscriber_link_source", sa.String(length=80), nullable=True),
    )
    op.add_column(
        "referrals", sa.Column("subscriber_link_reason", sa.Text(), nullable=True)
    )
    op.create_foreign_key(
        "fk_referrals_referred_party_id",
        "referrals",
        "parties",
        ["referred_party_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_referrals_referred_party_id", "referrals", ["referred_party_id"]
    )
    op.create_index(
        "uq_referrals_active_referred_party",
        "referrals",
        ["referred_party_id"],
        unique=True,
        postgresql_where=sa.text("is_active AND referred_party_id IS NOT NULL"),
    )
    op.create_check_constraint(
        "ck_referrals_party_binding_evidence",
        "referrals",
        "(referred_party_id IS NULL AND party_bound_at IS NULL AND "
        "party_binding_source IS NULL AND party_binding_reason IS NULL) OR "
        "(referred_party_id IS NOT NULL AND party_bound_at IS NOT NULL AND "
        "party_binding_source IS NOT NULL AND party_binding_reason IS NOT NULL "
        "AND length(trim(party_binding_source)) > 0 AND "
        "length(trim(party_binding_reason)) > 0)",
    )
    op.create_check_constraint(
        "ck_referrals_subscriber_link_evidence",
        "referrals",
        "(referred_subscriber_id IS NULL AND subscriber_linked_at IS NULL AND "
        "subscriber_link_source IS NULL AND subscriber_link_reason IS NULL) OR "
        "(referred_subscriber_id IS NOT NULL AND subscriber_linked_at IS NULL AND "
        "subscriber_link_source IS NULL AND subscriber_link_reason IS NULL) OR "
        "(referred_subscriber_id IS NOT NULL AND subscriber_linked_at IS NOT NULL "
        "AND subscriber_link_source IS NOT NULL AND subscriber_link_reason IS NOT "
        "NULL AND length(trim(subscriber_link_source)) > 0 AND "
        "length(trim(subscriber_link_reason)) > 0)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_referrals_subscriber_link_evidence", "referrals", type_="check"
    )
    op.drop_constraint(
        "ck_referrals_party_binding_evidence", "referrals", type_="check"
    )
    op.drop_index("uq_referrals_active_referred_party", table_name="referrals")
    op.drop_index("ix_referrals_referred_party_id", table_name="referrals")
    op.drop_constraint(
        "fk_referrals_referred_party_id", "referrals", type_="foreignkey"
    )
    op.drop_column("referrals", "subscriber_link_reason")
    op.drop_column("referrals", "subscriber_link_source")
    op.drop_column("referrals", "subscriber_linked_at")
    op.drop_column("referrals", "party_binding_reason")
    op.drop_column("referrals", "party_binding_source")
    op.drop_column("referrals", "party_bound_at")
    op.drop_column("referrals", "referred_party_id")
