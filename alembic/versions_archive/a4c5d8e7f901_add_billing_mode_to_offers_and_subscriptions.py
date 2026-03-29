"""add_billing_mode_to_offers_and_subscriptions

Revision ID: a4c5d8e7f901
Revises: 3c20a91bc334
Create Date: 2026-01-29 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "a4c5d8e7f901"
down_revision = "3c20a91bc334"
branch_labels = None
depends_on = None


def upgrade() -> None:
    billing_mode_enum = sa.Enum("prepaid", "postpaid", name="billingmode")
    billing_mode_enum.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "catalog_offers",
        sa.Column(
            "billing_mode",
            billing_mode_enum,
            nullable=False,
            server_default="prepaid",
        ),
    )
    op.add_column(
        "subscriptions",
        sa.Column(
            "billing_mode",
            billing_mode_enum,
            nullable=False,
            server_default="prepaid",
        ),
    )
    op.alter_column("catalog_offers", "billing_mode", server_default=None)
    op.alter_column("subscriptions", "billing_mode", server_default=None)


def downgrade() -> None:
    op.drop_column("subscriptions", "billing_mode")
    op.drop_column("catalog_offers", "billing_mode")
    billing_mode_enum = sa.Enum("prepaid", "postpaid", name="billingmode")
    billing_mode_enum.drop(op.get_bind(), checkfirst=True)
