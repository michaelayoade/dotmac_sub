"""VAS review fix: bind top-up references to their initiating wallet.

Closes the reference-theft hole: any authed user who learned another
customer's checkout reference could previously verify it into their own
wallet. Verify now requires a matching intent row.

Revision ID: 148_vas_topup_intents
Revises: 147_vas_rate_cards
Create Date: 2026-06-12
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "148_vas_topup_intents"
down_revision = "147_vas_rate_cards"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if not inspect(bind).has_table("vas_topup_intents"):
        op.create_table(
            "vas_topup_intents",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("reference", sa.String(120), nullable=False, unique=True),
            sa.Column(
                "wallet_id",
                UUID(as_uuid=True),
                sa.ForeignKey("vas_wallets.id"),
                nullable=False,
            ),
            sa.Column("amount", sa.Numeric(12, 2), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if inspect(bind).has_table("vas_topup_intents"):
        op.drop_table("vas_topup_intents")
