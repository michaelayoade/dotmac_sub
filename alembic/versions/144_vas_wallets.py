"""VAS wallet core: vas_wallets + vas_wallet_entries, 'vas' settings domain.

Customer-liability wallet, separate from the billing ledger by design —
see docs/designs/VTU_BILL_PAYMENTS.md.

Revision ID: 144_vas_wallets
Revises: 143_customer_location_change_requests
Create Date: 2026-06-12
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "144_vas_wallets"
down_revision = "143_customer_location_change_requests"
branch_labels = None
depends_on = None

_ENTRY_TYPE = sa.Enum("credit", "debit", name="vasentrytype")
_ENTRY_CATEGORY = sa.Enum(
    "topup",
    "purchase",
    "purchase_refund",
    "bill_payment",
    "commission",
    "adjustment",
    name="vasentrycategory",
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TYPE settingdomain ADD VALUE IF NOT EXISTS 'vas'")

    inspector = inspect(bind)
    if not inspector.has_table("vas_wallets"):
        op.create_table(
            "vas_wallets",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "subscriber_id",
                UUID(as_uuid=True),
                sa.ForeignKey("subscribers.id"),
                unique=True,
            ),
            sa.Column(
                "reseller_id",
                UUID(as_uuid=True),
                sa.ForeignKey("resellers.id"),
                unique=True,
            ),
            sa.Column(
                "auto_pay_bill_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
            sa.Column(
                "is_active", sa.Boolean(), nullable=False, server_default=sa.true()
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "(subscriber_id IS NOT NULL AND reseller_id IS NULL)"
                " OR (subscriber_id IS NULL AND reseller_id IS NOT NULL)",
                name="ck_vas_wallets_exactly_one_owner",
            ),
        )

    if not inspector.has_table("vas_wallet_entries"):
        op.create_table(
            "vas_wallet_entries",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "wallet_id",
                UUID(as_uuid=True),
                sa.ForeignKey("vas_wallets.id"),
                nullable=False,
            ),
            sa.Column("entry_type", _ENTRY_TYPE, nullable=False),
            sa.Column("category", _ENTRY_CATEGORY, nullable=False),
            sa.Column("amount", sa.Numeric(12, 2), nullable=False),
            sa.Column("currency", sa.String(3), nullable=False, server_default="NGN"),
            sa.Column("reference", sa.String(120), unique=True),
            sa.Column("payment_id", UUID(as_uuid=True), sa.ForeignKey("payments.id")),
            sa.Column("memo", sa.Text()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "amount > 0", name="ck_vas_wallet_entries_amount_positive"
            ),
        )
        op.create_index(
            "ix_vas_wallet_entries_wallet_created",
            "vas_wallet_entries",
            ["wallet_id", "created_at"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if inspector.has_table("vas_wallet_entries"):
        op.drop_index(
            "ix_vas_wallet_entries_wallet_created", table_name="vas_wallet_entries"
        )
        op.drop_table("vas_wallet_entries")
    if inspector.has_table("vas_wallets"):
        op.drop_table("vas_wallets")
    _ENTRY_TYPE.drop(bind, checkfirst=True)
    _ENTRY_CATEGORY.drop(bind, checkfirst=True)
    # The 'vas' settingdomain enum value is left in place (PG cannot drop
    # enum values without a type rebuild).
