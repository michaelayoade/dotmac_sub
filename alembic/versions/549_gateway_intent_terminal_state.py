"""Persist effective expiry for legacy pending gateway intents.

Revision ID: 549_gateway_intent_terminal_state
Revises: 548_inbox_observation_quarantine
Create Date: 2026-08-22
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "549_gateway_intent_terminal_state"
down_revision: str | None = "548_inbox_observation_quarantine"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Backfill only elapsed, unsettled intents for supported gateways."""

    op.execute(
        """
        UPDATE topup_intents
        SET status = 'expired', updated_at = CURRENT_TIMESTAMP
        WHERE status = 'pending'
          AND completed_payment_id IS NULL
          AND provider_type IN ('paystack', 'flutterwave')
          AND expires_at IS NOT NULL
          AND expires_at <= CURRENT_TIMESTAMP
        """
    )


def downgrade() -> None:
    """Do not reopen expired payment attempts during rollback."""

    # Expired rows are indistinguishable from rows expired by the lifecycle
    # owner after upgrade. Reopening either set would restore a duplicate-payment
    # blocker and misstate elapsed attempts, so this data repair is forward-only.
    pass
