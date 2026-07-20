"""Add CRM inbound webhook delivery idempotency table.

Revision ID: 205_add_crm_webhook_deliveries
Revises: 204_add_reseller_restrict_to_assigned_offers
Create Date: 2026-07-04
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "205_add_crm_webhook_deliveries"
down_revision = "204_add_reseller_restrict_to_assigned_offers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "crm_webhook_deliveries",
        sa.Column("delivery_id", sa.UUID(), nullable=False),
        sa.Column("event_id", sa.String(length=120), nullable=True),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("crm_ticket_id", sa.String(length=80), nullable=True),
        sa.Column("crm_comment_id", sa.String(length=80), nullable=True),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("result", sa.String(length=80), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("delivery_id"),
    )
    op.create_index(
        "ix_crm_webhook_deliveries_event_type",
        "crm_webhook_deliveries",
        ["event_type"],
    )
    op.create_index(
        "ix_crm_webhook_deliveries_crm_ticket_id",
        "crm_webhook_deliveries",
        ["crm_ticket_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_crm_webhook_deliveries_crm_ticket_id",
        table_name="crm_webhook_deliveries",
    )
    op.drop_index(
        "ix_crm_webhook_deliveries_event_type",
        table_name="crm_webhook_deliveries",
    )
    op.drop_table("crm_webhook_deliveries")
