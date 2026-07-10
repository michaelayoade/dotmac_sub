"""Add prepaid service entitlements.

Revision ID: 240_add_service_entitlements
Revises: 239_field_job_chat
Create Date: 2026-07-10
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "240_add_service_entitlements"
down_revision = "239_field_job_chat"
branch_labels = None
depends_on = None

_TABLE = "service_entitlements"


def _has_table(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    if _has_table(_TABLE):
        return
    op.create_table(
        _TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscribers.id"),
            nullable=False,
        ),
        sa.Column(
            "subscription_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscriptions.id"),
            nullable=False,
        ),
        sa.Column(
            "source_invoice_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("invoices.id"),
        ),
        sa.Column(
            "source_invoice_line_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("invoice_lines.id"),
        ),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "amount_funded",
            sa.Numeric(12, 2),
            nullable=False,
            server_default="0.00",
        ),
        sa.Column(
            "currency", sa.String(length=3), nullable=False, server_default="NGN"
        ),
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default="active"
        ),
        sa.Column("metadata", postgresql.JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_service_entitlements_account_subscription_period",
        _TABLE,
        ["account_id", "subscription_id", "starts_at", "ends_at"],
    )
    op.create_index(
        "uq_service_entitlements_active_invoice_line",
        _TABLE,
        ["source_invoice_line_id"],
        unique=True,
        postgresql_where=sa.text(
            "status = 'active' AND source_invoice_line_id IS NOT NULL"
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    if not _has_table(_TABLE):
        return
    op.drop_index("uq_service_entitlements_active_invoice_line", table_name=_TABLE)
    op.drop_index(
        "ix_service_entitlements_account_subscription_period", table_name=_TABLE
    )
    op.drop_table(_TABLE)
