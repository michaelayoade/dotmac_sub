"""Add top-up intents for secure customer portal funding flow.

Revision ID: 104_add_topup_intents
Revises: 103_add_admin_whats_new_items
Create Date: 2026-05-24
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "104_add_topup_intents"
down_revision = "103_add_admin_whats_new_items"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "topup_intents",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False
        ),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscribers.id"),
            nullable=False,
        ),
        sa.Column(
            "completed_payment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("payments.id"),
            nullable=True,
        ),
        sa.Column("reference", sa.String(length=120), nullable=False),
        sa.Column("provider_type", sa.String(length=40), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("requested_amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("actual_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("external_id", sa.String(length=120), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_topup_intents_account_id",
        "topup_intents",
        ["account_id"],
        unique=False,
    )
    op.create_index(
        "uq_topup_intents_reference",
        "topup_intents",
        ["reference"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_topup_intents_reference", table_name="topup_intents")
    op.drop_index("ix_topup_intents_account_id", table_name="topup_intents")
    op.drop_table("topup_intents")
