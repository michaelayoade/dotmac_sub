"""Add offer availability controls, ledger category, and discount date fields.

Closes migration gaps:
- 4 junction tables for offer availability (reseller, location, category, billing mode)
- LedgerCategory enum + category column on ledger_entries
- discount_start_at, discount_end_at, discount_description on subscriptions

Revision ID: n2o3p4q5r6s7
Revises: m1n2o3p4q5r6
Create Date: 2026-03-15 14:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision = "n2o3p4q5r6s7"
down_revision = "m1n2o3p4q5r6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = inspect(conn)
    existing_tables = set(inspector.get_table_names())

    # --- Enum types (idempotent) ---
    ledger_cat_enum = postgresql.ENUM(
        "internet_service",
        "custom_service",
        "voice_service",
        "bundle_service",
        "installation_fee",
        "equipment_rental",
        "equipment_purchase",
        "late_payment_fee",
        "reconnection_fee",
        "deposit",
        "discount",
        "tax",
        "overage",
        "top_up",
        "other",
        name="ledgercategory",
        create_type=False,
    )
    ledger_cat_enum.create(conn, checkfirst=True)

    # subscribercategory enum should already exist from subscribers
    sub_cat_enum = postgresql.ENUM(
        "residential",
        "business",
        "government",
        "ngo",
        name="subscribercategory",
        create_type=False,
    )
    sub_cat_enum.create(conn, checkfirst=True)

    # billingmode enum should already exist from subscriptions
    billing_mode_enum = postgresql.ENUM(
        "prepaid",
        "postpaid",
        name="billingmode",
        create_type=False,
    )
    billing_mode_enum.create(conn, checkfirst=True)

    # --- 1. offer_reseller_availability ---
    if "offer_reseller_availability" not in existing_tables:
        op.create_table(
            "offer_reseller_availability",
            sa.Column(
                "id", postgresql.UUID(as_uuid=True), primary_key=True
            ),
            sa.Column(
                "offer_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("catalog_offers.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "reseller_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("resellers.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "is_active", sa.Boolean, server_default=sa.text("true")
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.UniqueConstraint(
                "offer_id", "reseller_id", name="uq_offer_reseller"
            ),
        )

    # --- 2. offer_location_availability ---
    if "offer_location_availability" not in existing_tables:
        op.create_table(
            "offer_location_availability",
            sa.Column(
                "id", postgresql.UUID(as_uuid=True), primary_key=True
            ),
            sa.Column(
                "offer_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("catalog_offers.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "pop_site_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("pop_sites.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "is_active", sa.Boolean, server_default=sa.text("true")
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.UniqueConstraint(
                "offer_id", "pop_site_id", name="uq_offer_location"
            ),
        )

    # --- 3. offer_category_availability ---
    if "offer_category_availability" not in existing_tables:
        op.create_table(
            "offer_category_availability",
            sa.Column(
                "id", postgresql.UUID(as_uuid=True), primary_key=True
            ),
            sa.Column(
                "offer_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("catalog_offers.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "subscriber_category",
                postgresql.ENUM(
                    "residential",
                    "business",
                    "government",
                    "ngo",
                    name="subscribercategory",
                    create_type=False,
                ),
                nullable=False,
            ),
            sa.Column(
                "is_active", sa.Boolean, server_default=sa.text("true")
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.UniqueConstraint(
                "offer_id",
                "subscriber_category",
                name="uq_offer_category",
            ),
        )

    # --- 4. offer_billing_mode_availability ---
    if "offer_billing_mode_availability" not in existing_tables:
        op.create_table(
            "offer_billing_mode_availability",
            sa.Column(
                "id", postgresql.UUID(as_uuid=True), primary_key=True
            ),
            sa.Column(
                "offer_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("catalog_offers.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "billing_mode",
                postgresql.ENUM(
                    "prepaid",
                    "postpaid",
                    name="billingmode",
                    create_type=False,
                ),
                nullable=False,
            ),
            sa.Column(
                "is_active", sa.Boolean, server_default=sa.text("true")
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.UniqueConstraint(
                "offer_id", "billing_mode", name="uq_offer_billing_mode"
            ),
        )

    # --- 5. Add category column to ledger_entries ---
    existing_cols = {c["name"] for c in inspector.get_columns("ledger_entries")}
    if "category" not in existing_cols:
        op.add_column(
            "ledger_entries",
            sa.Column(
                "category",
                postgresql.ENUM(
                    "internet_service",
                    "custom_service",
                    "voice_service",
                    "bundle_service",
                    "installation_fee",
                    "equipment_rental",
                    "equipment_purchase",
                    "late_payment_fee",
                    "reconnection_fee",
                    "deposit",
                    "discount",
                    "tax",
                    "overage",
                    "top_up",
                    "other",
                    name="ledgercategory",
                    create_type=False,
                ),
                nullable=True,
            ),
        )

    # --- 6. Add discount date fields to subscriptions ---
    sub_cols = {c["name"] for c in inspector.get_columns("subscriptions")}
    if "discount_start_at" not in sub_cols:
        op.add_column(
            "subscriptions",
            sa.Column(
                "discount_start_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
        )
    if "discount_end_at" not in sub_cols:
        op.add_column(
            "subscriptions",
            sa.Column(
                "discount_end_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
        )
    if "discount_description" not in sub_cols:
        op.add_column(
            "subscriptions",
            sa.Column(
                "discount_description",
                sa.String(512),
                nullable=True,
            ),
        )


def downgrade() -> None:
    # Remove subscription discount fields
    op.drop_column("subscriptions", "discount_description")
    op.drop_column("subscriptions", "discount_end_at")
    op.drop_column("subscriptions", "discount_start_at")

    # Remove ledger category
    op.drop_column("ledger_entries", "category")

    # Drop availability tables
    op.drop_table("offer_billing_mode_availability")
    op.drop_table("offer_category_availability")
    op.drop_table("offer_location_availability")
    op.drop_table("offer_reseller_availability")

    # Drop enum (only ledgercategory is new — others pre-existed)
    postgresql.ENUM(name="ledgercategory").drop(
        op.get_bind(), checkfirst=True
    )
