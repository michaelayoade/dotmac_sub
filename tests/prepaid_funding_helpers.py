"""Test helpers for the irreversible prepaid opening-balance contract."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from app.models.prepaid_funding import PrepaidFundingReconstructionBatch
from app.services import prepaid_funding_attestation
from app.services.prepaid_funding_reconstruction import (
    apply_prepaid_funding_reconstruction,
    parse_reconstruction_manifest,
)
from tests.prepaid_funding_test_support import (
    ephemeral_public_signing_key_pem,
    sealed_reconstruction_payload,
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
    db.query(PrepaidFundingReconstructionBatch).filter(
        PrepaidFundingReconstructionBatch.source
        == "pytest-empty-native-install-cutover",
        PrepaidFundingReconstructionBatch.account_count == 0,
    ).delete(synchronize_session=False)
    db.flush()
    payload = sealed_reconstruction_payload(
        position_at,
        balances,
        source="pytest-reviewed-opening-balance",
    )
    digest = parse_reconstruction_manifest(payload["manifest"]).manifest_sha256
    with patch.object(
        prepaid_funding_attestation,
        "resolve_trusted_public_key_pem",
        return_value=ephemeral_public_signing_key_pem(),
    ):
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
