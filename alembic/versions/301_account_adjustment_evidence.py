"""Add previewed account-adjustment and add-on debit evidence.

Revision ID: 301_account_adjustment_evidence
Revises: 300_retire_vas_runtime
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "301_account_adjustment_evidence"
down_revision = "300_retire_vas_runtime"
branch_labels = None
depends_on = None

_ledger_category = postgresql.ENUM(
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


def upgrade() -> None:
    op.create_table(
        "account_adjustments",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True
        ),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("category", _ledger_category, nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("memo", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("origin", sa.String(40), nullable=False),
        sa.Column("origin_ref", sa.String(160), nullable=True),
        sa.Column("prepaid_funding_before", sa.Numeric(12, 2), nullable=False),
        sa.Column("prepaid_funding_after", sa.Numeric(12, 2), nullable=False),
        sa.Column(
            "postpaid_receivables",
            sa.Numeric(12, 2),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "collection_blocking_balance",
            sa.Numeric(12, 2),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "access_consequence",
            sa.String(80),
            nullable=False,
            server_default="none_adjustment_only",
        ),
        sa.Column("preview_fingerprint", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(120), nullable=False),
        sa.Column("ledger_entry_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "reversal_ledger_entry_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("reversal_preview_fingerprint", sa.String(64), nullable=True),
        sa.Column("reversal_idempotency_key", sa.String(120), nullable=True),
        sa.Column("reversal_reason", sa.Text(), nullable=True),
        sa.Column("reversal_prepaid_funding_before", sa.Numeric(12, 2), nullable=True),
        sa.Column("reversal_prepaid_funding_after", sa.Numeric(12, 2), nullable=True),
        sa.Column("reversed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount > 0", name="ck_account_adjustments_amount_positive"),
        sa.ForeignKeyConstraint(
            ["account_id"], ["subscribers.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["ledger_entry_id"], ["ledger_entries.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["reversal_ledger_entry_id"],
            ["ledger_entries.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "origin",
            "idempotency_key",
            name="uq_account_adjustments_origin_idempotency",
        ),
        sa.UniqueConstraint(
            "origin",
            "reversal_idempotency_key",
            name="uq_account_adjustments_origin_reversal_idempotency",
        ),
    )
    op.create_index(
        "ix_account_adjustments_account_id",
        "account_adjustments",
        ["account_id"],
    )
    op.create_index(
        "uq_account_adjustments_ledger_entry_id",
        "account_adjustments",
        ["ledger_entry_id"],
        unique=True,
    )
    op.create_index(
        "uq_account_adjustments_reversal_ledger_entry_id",
        "account_adjustments",
        ["reversal_ledger_entry_id"],
        unique=True,
    )

    op.add_column(
        "subscription_add_ons",
        sa.Column(
            "account_adjustment_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
    )
    op.add_column(
        "subscription_add_ons",
        sa.Column("purchase_preview_fingerprint", sa.String(64), nullable=True),
    )
    op.add_column(
        "subscription_add_ons",
        sa.Column("purchase_idempotency_key", sa.String(120), nullable=True),
    )
    op.create_foreign_key(
        "fk_subscription_add_ons_account_adjustment_id",
        "subscription_add_ons",
        "account_adjustments",
        ["account_adjustment_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "uq_subscription_add_ons_account_adjustment_id",
        "subscription_add_ons",
        ["account_adjustment_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_subscription_add_ons_account_adjustment_id",
        table_name="subscription_add_ons",
    )
    op.drop_constraint(
        "fk_subscription_add_ons_account_adjustment_id",
        "subscription_add_ons",
        type_="foreignkey",
    )
    op.drop_column("subscription_add_ons", "purchase_idempotency_key")
    op.drop_column("subscription_add_ons", "purchase_preview_fingerprint")
    op.drop_column("subscription_add_ons", "account_adjustment_id")

    op.drop_index(
        "uq_account_adjustments_reversal_ledger_entry_id",
        table_name="account_adjustments",
    )
    op.drop_index(
        "uq_account_adjustments_ledger_entry_id",
        table_name="account_adjustments",
    )
    op.drop_index("ix_account_adjustments_account_id", table_name="account_adjustments")
    op.drop_table("account_adjustments")
