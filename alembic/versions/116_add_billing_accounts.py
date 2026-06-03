"""Add billing accounts, house reseller, consolidated payments.

Revision ID: 116_add_billing_accounts
Revises: 115_add_bandwidth_samples_subscription_sample_at_index
Create Date: 2026-06-03
"""

from __future__ import annotations

import logging

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "116_add_billing_accounts"
down_revision = "115_add_bandwidth_samples_subscription_sample_at_index"
branch_labels = None
depends_on = None

logger = logging.getLogger(__name__)

_HOUSE_RESELLER_NAME = "House"
_HOUSE_RESELLER_CODE = "HOUSE"


def _columns(inspector, table: str) -> set[str]:
    return {c["name"] for c in inspector.get_columns(table)}


def _indexes(inspector, table: str) -> set[str]:
    return {i["name"] for i in inspector.get_indexes(table)}


def _fk_names(inspector, table: str) -> set[str]:
    return {fk["name"] for fk in inspector.get_foreign_keys(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    # 1. resellers.is_house column
    reseller_cols = _columns(inspector, "resellers")
    if "is_house" not in reseller_cols:
        op.add_column(
            "resellers",
            sa.Column(
                "is_house",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )

    # 2. Partial unique index ensuring at most one house reseller
    reseller_indexes = _indexes(inspector, "resellers")
    if "uq_resellers_one_house" not in reseller_indexes:
        op.create_index(
            "uq_resellers_one_house",
            "resellers",
            ["is_house"],
            unique=True,
            postgresql_where=sa.text("is_house"),
        )

    # 3. Ensure a House reseller row exists
    house_id = bind.execute(
        sa.text("SELECT id FROM resellers WHERE is_house = true LIMIT 1")
    ).scalar()
    if house_id is None:
        house_id = bind.execute(
            sa.text(
                """
                INSERT INTO resellers (id, name, code, is_active, is_house, created_at, updated_at)
                VALUES (gen_random_uuid(), :name, :code, true, true, now(), now())
                RETURNING id
                """
            ),
            {"name": _HOUSE_RESELLER_NAME, "code": _HOUSE_RESELLER_CODE},
        ).scalar()

    # 4. Backfill subscribers.reseller_id from NULL to House
    bind.execute(
        sa.text("UPDATE subscribers SET reseller_id = :h WHERE reseller_id IS NULL"),
        {"h": house_id},
    )

    # 5. Alter subscribers.reseller_id to NOT NULL (idempotent)
    sub_cols = inspector.get_columns("subscribers")
    sub_reseller_col = next(c for c in sub_cols if c["name"] == "reseller_id")
    if sub_reseller_col.get("nullable", True):
        op.alter_column(
            "subscribers",
            "reseller_id",
            existing_type=postgresql.UUID(as_uuid=True),
            nullable=False,
        )

    # 6. Create billing_accounts table
    if "billing_accounts" not in inspector.get_table_names():
        op.create_table(
            "billing_accounts",
            sa.Column(
                "id",
                postgresql.UUID(as_uuid=True),
                primary_key=True,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column(
                "reseller_id",
                postgresql.UUID(as_uuid=True),
                nullable=False,
                unique=True,
            ),
            sa.Column("name", sa.String(length=160), nullable=False),
            sa.Column(
                "currency", sa.String(length=3), nullable=False, server_default="NGN"
            ),
            sa.Column(
                "status", sa.String(length=20), nullable=False, server_default="active"
            ),
            sa.Column(
                "balance",
                sa.Numeric(12, 2),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column(
                "is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")
            ),
            sa.Column(
                "metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.ForeignKeyConstraint(
                ["reseller_id"],
                ["resellers.id"],
                name="fk_billing_accounts_reseller_id",
            ),
        )

    # 7. Backfill one BillingAccount per reseller that doesn't have one
    bind.execute(
        sa.text(
            """
            INSERT INTO billing_accounts (id, reseller_id, name, currency, status, balance, is_active, created_at, updated_at)
            SELECT gen_random_uuid(), r.id, r.name, 'NGN', 'active', 0, true, now(), now()
              FROM resellers r
             WHERE NOT EXISTS (
                       SELECT 1 FROM billing_accounts b WHERE b.reseller_id = r.id
                   )
            """
        )
    )

    # 8. payments: billing_account_id + nullable account_id
    payment_cols_list = inspector.get_columns("payments")
    payment_col_names = {c["name"] for c in payment_cols_list}
    if "billing_account_id" not in payment_col_names:
        op.add_column(
            "payments",
            sa.Column(
                "billing_account_id",
                postgresql.UUID(as_uuid=True),
                nullable=True,
            ),
        )
    payment_account_col = next(c for c in payment_cols_list if c["name"] == "account_id")
    if not payment_account_col.get("nullable", True):
        op.alter_column(
            "payments",
            "account_id",
            existing_type=postgresql.UUID(as_uuid=True),
            nullable=True,
        )
    payment_fks = _fk_names(inspector, "payments")
    if "fk_payments_billing_account_id" not in payment_fks:
        op.create_foreign_key(
            "fk_payments_billing_account_id",
            "payments",
            "billing_accounts",
            ["billing_account_id"],
            ["id"],
        )

    # 9. topup_intents: nullable account_id + new billing_account_id
    topup_cols = inspector.get_columns("topup_intents")
    topup_account_col = next(c for c in topup_cols if c["name"] == "account_id")
    if not topup_account_col.get("nullable", True):
        op.alter_column(
            "topup_intents",
            "account_id",
            existing_type=postgresql.UUID(as_uuid=True),
            nullable=True,
        )
    topup_col_names = {c["name"] for c in topup_cols}
    if "billing_account_id" not in topup_col_names:
        op.add_column(
            "topup_intents",
            sa.Column(
                "billing_account_id",
                postgresql.UUID(as_uuid=True),
                nullable=True,
            ),
        )
    topup_fks = _fk_names(inspector, "topup_intents")
    if "fk_topup_intents_billing_account_id" not in topup_fks:
        op.create_foreign_key(
            "fk_topup_intents_billing_account_id",
            "topup_intents",
            "billing_accounts",
            ["billing_account_id"],
            ["id"],
        )


def downgrade() -> None:
    """Reverse the schema additions. Data rows (House Reseller, BillingAccount
    backfills) are intentionally NOT deleted so that re-applying the migration
    is idempotent and no payment history is lost."""
    bind = op.get_bind()
    inspector = inspect(bind)

    topup_fks = _fk_names(inspector, "topup_intents")
    if "fk_topup_intents_billing_account_id" in topup_fks:
        op.drop_constraint(
            "fk_topup_intents_billing_account_id", "topup_intents", type_="foreignkey"
        )
    if "billing_account_id" in _columns(inspector, "topup_intents"):
        op.drop_column("topup_intents", "billing_account_id")

    payment_fks = _fk_names(inspector, "payments")
    if "fk_payments_billing_account_id" in payment_fks:
        op.drop_constraint(
            "fk_payments_billing_account_id", "payments", type_="foreignkey"
        )
    if "billing_account_id" in _columns(inspector, "payments"):
        op.drop_column("payments", "billing_account_id")

    if "billing_accounts" in inspector.get_table_names():
        logger.warning(
            "Dropping billing_accounts table; consolidated payment history will lose "
            "its billing_account_id linkage."
        )
        op.drop_table("billing_accounts")

    if "uq_resellers_one_house" in _indexes(inspector, "resellers"):
        op.drop_index("uq_resellers_one_house", table_name="resellers")

    if "is_house" in _columns(inspector, "resellers"):
        op.drop_column("resellers", "is_house")
    # NOTE: subscribers.reseller_id remains NOT NULL — reverting to nullable
    # is unsafe if downstream code now assumes the invariant.
