"""VAS Phase 3: effective-dated commission rate cards.

Revision ID: 147_vas_rate_cards
Revises: 146_vas_hardening
Create Date: 2026-06-12
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "147_vas_rate_cards"
down_revision = "146_vas_hardening"
branch_labels = None
depends_on = None

_PARTY = sa.Enum("owner", "reseller", name="vaspartytype")


def upgrade() -> None:
    bind = op.get_bind()
    if not inspect(bind).has_table("vas_rate_cards"):
        op.create_table(
            "vas_rate_cards",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("category", sa.String(60), nullable=False),
            sa.Column("party_type", _PARTY, nullable=False),
            sa.Column("rate_pct", sa.Numeric(7, 4), nullable=False),
            sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
            sa.Column("memo", sa.Text()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(
            "ix_vas_rate_cards_lookup",
            "vas_rate_cards",
            ["category", "party_type", "effective_from"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    if inspect(bind).has_table("vas_rate_cards"):
        op.drop_index("ix_vas_rate_cards_lookup", table_name="vas_rate_cards")
        op.drop_table("vas_rate_cards")
    _PARTY.drop(bind, checkfirst=True)
