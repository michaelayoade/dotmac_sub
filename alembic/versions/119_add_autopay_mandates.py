"""Add autopay_mandates — per-account opt-in to auto-charge a saved card.

Revision ID: 119_add_autopay_mandates
Revises: 117_add_payment_webhook_dead_letters
Create Date: 2026-06-08

One mandate per account (unique account_id), pointing at the saved card to
charge on due invoices. Isolated table so existing billing queries are
unaffected.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "119_add_autopay_mandates"
down_revision = "117_add_payment_webhook_dead_letters"
branch_labels = None
depends_on = None

_TABLE = "autopay_mandates"


def upgrade() -> None:
    bind = op.get_bind()
    if _TABLE in inspect(bind).get_table_names():
        return
    op.create_table(
        _TABLE,
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscribers.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "payment_method_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("payment_methods.id"),
            nullable=True,
        ),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_table(_TABLE)
