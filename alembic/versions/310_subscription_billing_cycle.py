"""Own billing cadence on the subscription (bill per sales-order contract).

Revision ID: 310_subscription_billing_cycle
Revises: 309_retire_feature_aliases

Adds ``subscriptions.billing_cycle`` so a customer's contracted billing cadence
is owned by the subscription (SOT), with the offer price as fallback. Backfills
every existing subscription to the cadence the biller resolves TODAY (newest
active recurring version price -> offer price -> offer header -> monthly), so no
existing subscription's effective billing changes at cutover. Only new contracts
may set a cadence that differs from their offer price.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "310_subscription_billing_cycle"
down_revision = "309_retire_feature_aliases"
branch_labels = None
depends_on = None

# Reuse the existing PG enum type; a generic sa.Enum would emit CREATE TYPE and
# fail with DuplicateObject on a DB that already has ``billingcycle``.
_billingcycle = postgresql.ENUM(name="billingcycle", create_type=False)

# Backfill mirrors app.services.billing_automation._resolve_price (the money
# path): newest active recurring version price -> offer price -> offer header
# -> monthly. Reproduces each subscription's currently-billed cadence exactly.
_BACKFILL = sa.text(
    """
    UPDATE subscriptions s
    SET billing_cycle = COALESCE(
        (
            SELECT ovp.billing_cycle
            FROM offer_version_prices ovp
            WHERE ovp.offer_version_id = s.offer_version_id
              AND ovp.price_type = 'recurring'
              AND ovp.is_active = true
              AND ovp.billing_cycle IS NOT NULL
            ORDER BY ovp.created_at DESC, ovp.id DESC
            LIMIT 1
        ),
        (
            SELECT op.billing_cycle
            FROM offer_prices op
            WHERE op.offer_id = s.offer_id
              AND op.price_type = 'recurring'
              AND op.is_active = true
              AND op.billing_cycle IS NOT NULL
            ORDER BY op.created_at DESC, op.id DESC
            LIMIT 1
        ),
        (
            SELECT co.billing_cycle
            FROM catalog_offers co
            WHERE co.id = s.offer_id
        ),
        'monthly'::billingcycle
    )
    WHERE s.billing_cycle IS NULL
    """
)


def upgrade() -> None:
    op.add_column(
        "subscriptions",
        sa.Column("billing_cycle", _billingcycle, nullable=True),
    )
    op.execute(_BACKFILL)


def downgrade() -> None:
    op.drop_column("subscriptions", "billing_cycle")
