"""Add structural service-relocation intent and quote evidence.

Revision ID: 398_subscription_relocation_intent
Revises: 397_validate_payment_prepaid_archive
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "398_subscription_relocation_intent"
down_revision = "397_validate_payment_prepaid_archive"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subscription_change_requests",
        sa.Column(
            "target_service_address_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
    )
    op.add_column(
        "subscription_change_requests",
        sa.Column(
            "service_qualification_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
    )
    op.add_column(
        "subscription_change_requests",
        sa.Column("field_fee_offer_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "subscription_change_requests",
        sa.Column("field_fee_amount", sa.Numeric(12, 2), nullable=True),
    )
    op.add_column(
        "subscription_change_requests",
        sa.Column("field_fee_currency", sa.String(length=3), nullable=True),
    )
    op.add_column(
        "subscription_change_requests",
        sa.Column("field_quote_fingerprint", sa.String(length=64), nullable=True),
    )
    op.create_foreign_key(
        "fk_subscription_change_target_service_address",
        "subscription_change_requests",
        "addresses",
        ["target_service_address_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_subscription_change_service_qualification",
        "subscription_change_requests",
        "service_qualifications",
        ["service_qualification_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_subscription_change_field_fee_offer",
        "subscription_change_requests",
        "catalog_offers",
        ["field_fee_offer_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_subscription_change_field_fee_offer",
        "subscription_change_requests",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_subscription_change_service_qualification",
        "subscription_change_requests",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_subscription_change_target_service_address",
        "subscription_change_requests",
        type_="foreignkey",
    )
    for column in (
        "field_quote_fingerprint",
        "field_fee_currency",
        "field_fee_amount",
        "field_fee_offer_id",
        "service_qualification_id",
        "target_service_address_id",
    ):
        op.drop_column("subscription_change_requests", column)
