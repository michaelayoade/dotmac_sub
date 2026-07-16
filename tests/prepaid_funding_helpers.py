"""Test helpers for the irreversible prepaid opening-balance contract."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.services.prepaid_funding_reconstruction import (
    apply_prepaid_funding_reconstruction,
    parse_reconstruction_manifest,
)

TEST_PREPAID_POSITION_AT = datetime(2026, 3, 16, tzinfo=UTC)


def materialize_test_prepaid_opening_balance(
    db,
    account_id: object,
    amount: Decimal | str,
    *,
    position_at: datetime = TEST_PREPAID_POSITION_AT,
) -> None:
    """Create the same reviewed authority record production runtime requires."""
    materialize_test_prepaid_opening_balances(
        db,
        {account_id: amount},
        position_at=position_at,
    )


def materialize_test_prepaid_opening_balances(
    db,
    balances: dict[object, Decimal | str],
    *,
    position_at: datetime = TEST_PREPAID_POSITION_AT,
) -> None:
    """Materialize a complete test cohort in one reviewed batch."""
    payload = {
        "source": "pytest-reviewed-opening-balance",
        "position_at": position_at.isoformat().replace("+00:00", "Z"),
        "currency": "NGN",
        "accounts": [
            {
                "account_id": str(account_id),
                "available_balance": f"{Decimal(str(amount)):.2f}",
            }
            for account_id, amount in balances.items()
        ],
    }
    digest = parse_reconstruction_manifest(payload).manifest_sha256
    apply_prepaid_funding_reconstruction(
        db,
        payload,
        expected_manifest_sha256=digest,
        evidence_ref="pytest:prepaid-opening-balance",
        approved_by="pytest",
        expected_account_ids=set(balances),
        now=position_at + timedelta(minutes=1),
    )
    db.commit()
