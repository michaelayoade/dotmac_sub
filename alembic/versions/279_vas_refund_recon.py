"""Add durable VAS refund-to-source reconciliation state.

Revision ID: 279_vas_refund_recon
Revises: 278_campaign_parity
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "279_vas_refund_recon"
down_revision = "278_campaign_parity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vas_refund_requests",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "topup_entry_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("vas_wallet_entries.id"),
            nullable=False,
        ),
        sa.Column(
            "wallet_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("vas_wallets.id"),
            nullable=False,
        ),
        sa.Column(
            "wallet_debit_entry_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("vas_wallet_entries.id"),
            nullable=True,
        ),
        sa.Column(
            "wallet_reversal_entry_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("vas_wallet_entries.id"),
            nullable=True,
        ),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("funding_reference", sa.String(120), nullable=False),
        sa.Column("provider_transaction_id", sa.String(120), nullable=True),
        sa.Column("provider_refund_id", sa.String(120), nullable=True),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="NGN"),
        sa.Column("status", sa.String(32), nullable=False, server_default="prepared"),
        sa.Column("provider_status", sa.String(80), nullable=True),
        sa.Column("provider_response", sa.JSON(), nullable=True),
        sa.Column("submit_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "reconcile_attempts", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_reconciled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("amount > 0", name="ck_vas_refund_requests_amount_positive"),
        sa.UniqueConstraint(
            "topup_entry_id", name="uq_vas_refund_requests_topup_entry_id"
        ),
        sa.UniqueConstraint(
            "wallet_debit_entry_id",
            name="uq_vas_refund_requests_wallet_debit_entry_id",
        ),
        sa.UniqueConstraint(
            "wallet_reversal_entry_id",
            name="uq_vas_refund_requests_wallet_reversal_entry_id",
        ),
        sa.UniqueConstraint(
            "provider",
            "provider_refund_id",
            name="uq_vas_refund_requests_provider_refund",
        ),
    )
    op.create_index(
        "ix_vas_refund_requests_status_updated",
        "vas_refund_requests",
        ["status", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_vas_refund_requests_status_updated",
        table_name="vas_refund_requests",
    )
    op.drop_table("vas_refund_requests")
