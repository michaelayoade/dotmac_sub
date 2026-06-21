"""VAS Phase 2: VTPass catalog + purchase transactions.

Revision ID: 145_vas_catalog_transactions
Revises: 144_vas_wallets
Create Date: 2026-06-12
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "145_vas_catalog_transactions"
down_revision = "144_vas_wallets"
branch_labels = None
depends_on = None

_TXN_STATUS = sa.Enum(
    "pending",
    "debited",
    "submitted",
    "delivered",
    "failed",
    "refunded",
    "review",
    name="vastransactionstatus",
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if not inspector.has_table("vas_services"):
        op.create_table(
            "vas_services",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("category", sa.String(60), nullable=False, index=True),
            sa.Column("service_id", sa.String(120), nullable=False, unique=True),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("image_url", sa.String(400)),
            sa.Column("identifier_label", sa.String(120)),
            sa.Column(
                "requires_verify",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
            sa.Column(
                "is_enabled", sa.Boolean(), nullable=False, server_default=sa.false()
            ),
            sa.Column("min_amount", sa.Numeric(12, 2)),
            sa.Column("max_amount", sa.Numeric(12, 2)),
            sa.Column("raw", sa.JSON()),
            sa.Column("synced_at", sa.DateTime(timezone=True)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )

    if not inspector.has_table("vas_service_variations"):
        op.create_table(
            "vas_service_variations",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "service_pk",
                UUID(as_uuid=True),
                sa.ForeignKey("vas_services.id"),
                nullable=False,
            ),
            sa.Column("code", sa.String(120), nullable=False),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("amount", sa.Numeric(12, 2)),
            sa.Column(
                "is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()
            ),
            sa.Column("raw", sa.JSON()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "service_pk", "code", name="uq_vas_service_variations_service_code"
            ),
        )

    if not inspector.has_table("vas_transactions"):
        op.create_table(
            "vas_transactions",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "wallet_id",
                UUID(as_uuid=True),
                sa.ForeignKey("vas_wallets.id"),
                nullable=False,
            ),
            sa.Column(
                "subscriber_id", UUID(as_uuid=True), sa.ForeignKey("subscribers.id")
            ),
            sa.Column(
                "service_pk",
                UUID(as_uuid=True),
                sa.ForeignKey("vas_services.id"),
                nullable=False,
            ),
            sa.Column("variation_code", sa.String(120)),
            sa.Column("identifier", sa.String(120), nullable=False),
            sa.Column("amount", sa.Numeric(12, 2), nullable=False),
            sa.Column("request_id", sa.String(120), nullable=False, unique=True),
            sa.Column("status", _TXN_STATUS, nullable=False, server_default="pending"),
            sa.Column(
                "requery_attempts", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column("provider_status", sa.String(120)),
            sa.Column("provider_response", sa.JSON()),
            sa.Column("token_encrypted", sa.Text()),
            sa.Column("error", sa.Text()),
            sa.Column("delivered_at", sa.DateTime(timezone=True)),
            sa.Column("refunded_at", sa.DateTime(timezone=True)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(
            "ix_vas_transactions_wallet_created",
            "vas_transactions",
            ["wallet_id", "created_at"],
        )
        op.create_index("ix_vas_transactions_status", "vas_transactions", ["status"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if inspector.has_table("vas_transactions"):
        op.drop_index("ix_vas_transactions_status", table_name="vas_transactions")
        op.drop_index(
            "ix_vas_transactions_wallet_created", table_name="vas_transactions"
        )
        op.drop_table("vas_transactions")
    if inspector.has_table("vas_service_variations"):
        op.drop_table("vas_service_variations")
    if inspector.has_table("vas_services"):
        op.drop_table("vas_services")
    _TXN_STATUS.drop(bind, checkfirst=True)
