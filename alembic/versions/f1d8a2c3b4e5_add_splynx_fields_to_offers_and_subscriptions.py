"""Add Splynx fields to offers, subscriptions, and accounts.

Revision ID: f1d8a2c3b4e5
Revises: dd4f107ba42b
Create Date: 2026-01-14 20:40:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "f1d8a2c3b4e5"
down_revision = "dd4f107ba42b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    offer_columns = {col["name"] for col in inspector.get_columns("catalog_offers")}
    subscription_columns = {col["name"] for col in inspector.get_columns("subscriptions")}
    account_columns = {col["name"] for col in inspector.get_columns("subscriber_accounts")}

    if "splynx_tariff_id" not in offer_columns:
        op.add_column("catalog_offers", sa.Column("splynx_tariff_id", sa.Integer(), nullable=True))
    if "splynx_service_name" not in offer_columns:
        op.add_column("catalog_offers", sa.Column("splynx_service_name", sa.String(length=160), nullable=True))
    if "splynx_tax_id" not in offer_columns:
        op.add_column("catalog_offers", sa.Column("splynx_tax_id", sa.Integer(), nullable=True))
    if "with_vat" not in offer_columns:
        op.add_column("catalog_offers", sa.Column("with_vat", sa.Boolean(), server_default=sa.text("false"), nullable=False))
    if "vat_percent" not in offer_columns:
        op.add_column("catalog_offers", sa.Column("vat_percent", sa.Numeric(5, 2), nullable=True))
    if "speed_download_mbps" not in offer_columns:
        op.add_column("catalog_offers", sa.Column("speed_download_mbps", sa.Integer(), nullable=True))
    if "speed_upload_mbps" not in offer_columns:
        op.add_column("catalog_offers", sa.Column("speed_upload_mbps", sa.Integer(), nullable=True))
    if "aggregation" not in offer_columns:
        op.add_column("catalog_offers", sa.Column("aggregation", sa.Integer(), nullable=True))
    if "priority" not in offer_columns:
        op.add_column("catalog_offers", sa.Column("priority", sa.String(length=40), nullable=True))
    if "available_for_services" not in offer_columns:
        op.add_column(
            "catalog_offers",
            sa.Column("available_for_services", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        )
    if "show_on_customer_portal" not in offer_columns:
        op.add_column(
            "catalog_offers",
            sa.Column("show_on_customer_portal", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        )

    if "splynx_service_id" not in subscription_columns:
        op.add_column("subscriptions", sa.Column("splynx_service_id", sa.Integer(), nullable=True))
    if "router_id" not in subscription_columns:
        op.add_column("subscriptions", sa.Column("router_id", sa.Integer(), nullable=True))
    if "service_description" not in subscription_columns:
        op.add_column("subscriptions", sa.Column("service_description", sa.Text(), nullable=True))
    if "quantity" not in subscription_columns:
        op.add_column("subscriptions", sa.Column("quantity", sa.Integer(), nullable=True))
    if "unit" not in subscription_columns:
        op.add_column("subscriptions", sa.Column("unit", sa.String(length=40), nullable=True))
    if "unit_price" not in subscription_columns:
        op.add_column("subscriptions", sa.Column("unit_price", sa.Numeric(12, 2), nullable=True))
    if "discount" not in subscription_columns:
        op.add_column("subscriptions", sa.Column("discount", sa.Boolean(), server_default=sa.text("false"), nullable=False))
    if "discount_value" not in subscription_columns:
        op.add_column("subscriptions", sa.Column("discount_value", sa.Numeric(12, 2), nullable=True))
    if "discount_type" not in subscription_columns:
        op.add_column("subscriptions", sa.Column("discount_type", sa.String(length=40), nullable=True))
    if "service_status_raw" not in subscription_columns:
        op.add_column("subscriptions", sa.Column("service_status_raw", sa.String(length=40), nullable=True))
    if "login" not in subscription_columns:
        op.add_column("subscriptions", sa.Column("login", sa.String(length=120), nullable=True))
    if "ipv4_address" not in subscription_columns:
        op.add_column("subscriptions", sa.Column("ipv4_address", sa.String(length=64), nullable=True))
    if "ipv6_address" not in subscription_columns:
        op.add_column("subscriptions", sa.Column("ipv6_address", sa.String(length=128), nullable=True))
    if "mac_address" not in subscription_columns:
        op.add_column("subscriptions", sa.Column("mac_address", sa.String(length=64), nullable=True))

    if "billing_enabled" not in account_columns:
        op.add_column("subscriber_accounts", sa.Column("billing_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False))
    if "billing_person" not in account_columns:
        op.add_column("subscriber_accounts", sa.Column("billing_person", sa.String(length=160), nullable=True))
    if "billing_street_1" not in account_columns:
        op.add_column("subscriber_accounts", sa.Column("billing_street_1", sa.String(length=160), nullable=True))
    if "billing_zip_code" not in account_columns:
        op.add_column("subscriber_accounts", sa.Column("billing_zip_code", sa.String(length=20), nullable=True))
    if "billing_city" not in account_columns:
        op.add_column("subscriber_accounts", sa.Column("billing_city", sa.String(length=80), nullable=True))
    if "deposit" not in account_columns:
        op.add_column("subscriber_accounts", sa.Column("deposit", sa.Numeric(12, 2), nullable=True))
    if "payment_method" not in account_columns:
        op.add_column("subscriber_accounts", sa.Column("payment_method", sa.String(length=80), nullable=True))
    if "billing_date" not in account_columns:
        op.add_column("subscriber_accounts", sa.Column("billing_date", sa.Integer(), nullable=True))
    if "billing_due" not in account_columns:
        op.add_column("subscriber_accounts", sa.Column("billing_due", sa.Integer(), nullable=True))
    if "grace_period" not in account_columns:
        op.add_column("subscriber_accounts", sa.Column("grace_period", sa.Integer(), nullable=True))
    if "min_balance" not in account_columns:
        op.add_column("subscriber_accounts", sa.Column("min_balance", sa.Numeric(12, 2), nullable=True))
    if "month_price" not in account_columns:
        op.add_column("subscriber_accounts", sa.Column("month_price", sa.Numeric(12, 2), nullable=True))


def downgrade() -> None:
    op.drop_column("subscriber_accounts", "month_price")
    op.drop_column("subscriber_accounts", "min_balance")
    op.drop_column("subscriber_accounts", "grace_period")
    op.drop_column("subscriber_accounts", "billing_due")
    op.drop_column("subscriber_accounts", "billing_date")
    op.drop_column("subscriber_accounts", "payment_method")
    op.drop_column("subscriber_accounts", "deposit")
    op.drop_column("subscriber_accounts", "billing_city")
    op.drop_column("subscriber_accounts", "billing_zip_code")
    op.drop_column("subscriber_accounts", "billing_street_1")
    op.drop_column("subscriber_accounts", "billing_person")
    op.drop_column("subscriber_accounts", "billing_enabled")

    op.drop_column("subscriptions", "mac_address")
    op.drop_column("subscriptions", "ipv6_address")
    op.drop_column("subscriptions", "ipv4_address")
    op.drop_column("subscriptions", "login")
    op.drop_column("subscriptions", "service_status_raw")
    op.drop_column("subscriptions", "discount_type")
    op.drop_column("subscriptions", "discount_value")
    op.drop_column("subscriptions", "discount")
    op.drop_column("subscriptions", "unit_price")
    op.drop_column("subscriptions", "unit")
    op.drop_column("subscriptions", "quantity")
    op.drop_column("subscriptions", "service_description")
    op.drop_column("subscriptions", "router_id")
    op.drop_column("subscriptions", "splynx_service_id")

    op.drop_column("catalog_offers", "show_on_customer_portal")
    op.drop_column("catalog_offers", "available_for_services")
    op.drop_column("catalog_offers", "priority")
    op.drop_column("catalog_offers", "aggregation")
    op.drop_column("catalog_offers", "speed_upload_mbps")
    op.drop_column("catalog_offers", "speed_download_mbps")
    op.drop_column("catalog_offers", "vat_percent")
    op.drop_column("catalog_offers", "with_vat")
    op.drop_column("catalog_offers", "splynx_tax_id")
    op.drop_column("catalog_offers", "splynx_service_name")
    op.drop_column("catalog_offers", "splynx_tariff_id")
