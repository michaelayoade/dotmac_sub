"""Add payment channels, collection accounts, and allocations.

Revision ID: m2n3o4p5q6r
Revises: l1m2n3o4p5q6
Create Date: 2026-01-25
"""

from alembic import op
from datetime import datetime, timezone
import uuid
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "m2n3o4p5q6r"
down_revision = "l1m2n3o4p5q6"
branch_labels = None
depends_on = None


payment_channel_type = postgresql.ENUM(
    "card",
    "bank_transfer",
    "cash",
    "check",
    "transfer",
    "other",
    name="payment_channel_type",
    create_type=False,
)
payment_channel_type._create_events = False

collection_account_type = postgresql.ENUM(
    "bank",
    "cash",
    "other",
    name="collection_account_type",
    create_type=False,
)
collection_account_type._create_events = False


def upgrade() -> None:
    postgresql.ENUM(
        "card",
        "bank_transfer",
        "cash",
        "check",
        "transfer",
        "other",
        name="payment_channel_type",
    ).create(op.get_bind(), checkfirst=True)
    postgresql.ENUM(
        "bank",
        "cash",
        "other",
        name="collection_account_type",
    ).create(op.get_bind(), checkfirst=True)

    op.create_table(
        "collection_accounts",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("account_type", collection_account_type, nullable=False),
        sa.Column("bank_name", sa.String(length=120), nullable=True),
        sa.Column("account_last4", sa.String(length=4), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="NGN"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("name", name="uq_collection_accounts_name"),
    )

    op.create_table(
        "payment_channels",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("channel_type", payment_channel_type, nullable=False),
        sa.Column("provider_id", sa.UUID(), sa.ForeignKey("payment_providers.id"), nullable=True),
        sa.Column(
            "default_collection_account_id",
            sa.UUID(),
            sa.ForeignKey("collection_accounts.id"),
            nullable=True,
        ),
        sa.Column("fee_rules", sa.JSON(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("name", name="uq_payment_channels_name"),
    )

    op.create_table(
        "payment_channel_accounts",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("channel_id", sa.UUID(), sa.ForeignKey("payment_channels.id"), nullable=False),
        sa.Column(
            "collection_account_id",
            sa.UUID(),
            sa.ForeignKey("collection_accounts.id"),
            nullable=False,
        ),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "channel_id",
            "collection_account_id",
            "currency",
            name="uq_payment_channel_accounts_channel_account_currency",
        ),
    )
    op.create_index(
        "ix_payment_channel_accounts_lookup",
        "payment_channel_accounts",
        ["channel_id", "currency", "is_default", "priority"],
    )

    op.add_column(
        "payment_methods",
        sa.Column("payment_channel_id", sa.UUID(), sa.ForeignKey("payment_channels.id")),
    )
    op.add_column(
        "payments",
        sa.Column("payment_channel_id", sa.UUID(), sa.ForeignKey("payment_channels.id")),
    )
    op.add_column(
        "payments",
        sa.Column(
            "collection_account_id",
            sa.UUID(),
            sa.ForeignKey("collection_accounts.id"),
        ),
    )

    op.create_table(
        "payment_allocations",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("payment_id", sa.UUID(), sa.ForeignKey("payments.id"), nullable=False),
        sa.Column("invoice_id", sa.UUID(), sa.ForeignKey("invoices.id"), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False, server_default="0.00"),
        sa.Column("memo", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "payment_id",
            "invoice_id",
            name="uq_payment_allocations_payment_invoice",
        ),
    )
    op.create_index(
        "ix_payment_allocations_invoice",
        "payment_allocations",
        ["invoice_id"],
    )

    # Backfill: create allocations for existing payments linked to invoices.
    bind = op.get_bind()
    allocation_table = sa.table(
        "payment_allocations",
        sa.column("id", sa.UUID()),
        sa.column("payment_id", sa.UUID()),
        sa.column("invoice_id", sa.UUID()),
        sa.column("amount", sa.Numeric(12, 2)),
        sa.column("memo", sa.Text()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    payments = bind.execute(
        sa.text(
            """
            SELECT p.id, p.invoice_id, p.amount, p.memo, p.created_at
            FROM payments p
            WHERE p.invoice_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM payment_allocations pa WHERE pa.payment_id = p.id
              )
            """
        )
    ).fetchall()
    if payments:
        bind.execute(
            allocation_table.insert(),
            [
                {
                    "id": uuid.uuid4(),
                    "payment_id": row[0],
                    "invoice_id": row[1],
                    "amount": row[2],
                    "memo": row[3],
                    "created_at": row[4] or datetime.now(timezone.utc),
                }
                for row in payments
            ],
        )

    # Backfill: create default collection accounts per currency seen in payments.
    currencies = [
        row[0]
        for row in bind.execute(
            sa.text("SELECT DISTINCT currency FROM payments WHERE currency IS NOT NULL")
        ).fetchall()
    ]
    if not currencies:
        currencies = ["NGN"]
    collection_table = sa.table(
        "collection_accounts",
        sa.column("id", sa.UUID()),
        sa.column("name", sa.String()),
        sa.column("account_type", collection_account_type),
        sa.column("bank_name", sa.String()),
        sa.column("account_last4", sa.String()),
        sa.column("currency", sa.String()),
        sa.column("is_active", sa.Boolean()),
        sa.column("notes", sa.Text()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    collection_accounts = {}
    for currency in currencies:
        name = f"Unassigned {currency}"
        existing = bind.execute(
            sa.text(
                "SELECT id FROM collection_accounts WHERE name = :name"
            ),
            {"name": name},
        ).fetchone()
        if existing:
            collection_accounts[currency] = existing[0]
            continue
        account_id = uuid.uuid4()
        bind.execute(
            collection_table.insert(),
            {
                "id": account_id,
                "name": name,
                "account_type": "bank",
                "bank_name": None,
                "account_last4": None,
                "currency": currency,
                "is_active": True,
                "notes": "Auto-created for backfill",
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            },
        )
        collection_accounts[currency] = account_id

    # Backfill: create one payment channel per provider and map currencies.
    providers = bind.execute(
        sa.text("SELECT id, name FROM payment_providers WHERE is_active = true")
    ).fetchall()
    channel_table = sa.table(
        "payment_channels",
        sa.column("id", sa.UUID()),
        sa.column("name", sa.String()),
        sa.column("channel_type", payment_channel_type),
        sa.column("provider_id", sa.UUID()),
        sa.column("default_collection_account_id", sa.UUID()),
        sa.column("fee_rules", sa.JSON()),
        sa.column("is_active", sa.Boolean()),
        sa.column("is_default", sa.Boolean()),
        sa.column("notes", sa.Text()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    mapping_table = sa.table(
        "payment_channel_accounts",
        sa.column("id", sa.UUID()),
        sa.column("channel_id", sa.UUID()),
        sa.column("collection_account_id", sa.UUID()),
        sa.column("currency", sa.String()),
        sa.column("priority", sa.Integer()),
        sa.column("is_default", sa.Boolean()),
        sa.column("is_active", sa.Boolean()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    for provider_id, provider_name in providers:
        channel = bind.execute(
            sa.text(
                """
                SELECT id FROM payment_channels WHERE provider_id = :provider_id
                """
            ),
            {"provider_id": provider_id},
        ).fetchone()
        if channel:
            channel_id = channel[0]
        else:
            channel_id = uuid.uuid4()
            bind.execute(
                channel_table.insert(),
                {
                    "id": channel_id,
                    "name": provider_name,
                    "channel_type": "other",
                    "provider_id": provider_id,
                    "default_collection_account_id": collection_accounts[currencies[0]],
                    "fee_rules": None,
                    "is_active": True,
                    "is_default": True,
                    "notes": "Auto-created for backfill",
                    "created_at": datetime.now(timezone.utc),
                    "updated_at": datetime.now(timezone.utc),
                },
            )
        for currency, account_id in collection_accounts.items():
            existing = bind.execute(
                sa.text(
                    """
                    SELECT 1 FROM payment_channel_accounts
                    WHERE channel_id = :channel_id
                      AND collection_account_id = :account_id
                      AND currency = :currency
                    """
                ),
                {
                    "channel_id": channel_id,
                    "account_id": account_id,
                    "currency": currency,
                },
            ).fetchone()
            if existing:
                continue
            bind.execute(
                mapping_table.insert(),
                {
                    "id": uuid.uuid4(),
                    "channel_id": channel_id,
                    "collection_account_id": account_id,
                    "currency": currency,
                    "priority": 0,
                    "is_default": True,
                    "is_active": True,
                    "created_at": datetime.now(timezone.utc),
                    "updated_at": datetime.now(timezone.utc),
                },
            )

    # Backfill: assign payment_channel_id + collection_account_id to existing payments.
    bind.execute(
        sa.text(
            """
            UPDATE payments
            SET payment_channel_id = pc.id
            FROM payment_channels pc
            WHERE payments.payment_channel_id IS NULL
              AND payments.provider_id = pc.provider_id
            """
        )
    )
    bind.execute(
        sa.text(
            """
            UPDATE payments
            SET collection_account_id = pca.collection_account_id
            FROM payment_channel_accounts pca
            WHERE payments.collection_account_id IS NULL
              AND payments.payment_channel_id = pca.channel_id
              AND payments.currency = pca.currency
            """
        )
    )


def downgrade() -> None:
    op.drop_index("ix_payment_allocations_invoice", table_name="payment_allocations")
    op.drop_table("payment_allocations")

    op.drop_column("payments", "collection_account_id")
    op.drop_column("payments", "payment_channel_id")
    op.drop_column("payment_methods", "payment_channel_id")

    op.drop_index("ix_payment_channel_accounts_lookup", table_name="payment_channel_accounts")
    op.drop_table("payment_channel_accounts")
    op.drop_table("payment_channels")
    op.drop_table("collection_accounts")

    postgresql.ENUM(
        "bank",
        "cash",
        "other",
        name="collection_account_type",
    ).drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(
        "card",
        "bank_transfer",
        "cash",
        "check",
        "transfer",
        "other",
        name="payment_channel_type",
    ).drop(op.get_bind(), checkfirst=True)
