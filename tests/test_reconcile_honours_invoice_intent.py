"""A recovered payment must settle the invoice the customer actually chose.

Invoice checkouts reuse ``TopupIntent`` as their durable trace, stamping
``metadata_["payment_flow"] = "invoice_payment"`` and ``metadata_["invoice_id"]``.
That intent is the authoritative record of what the payment was *for*.

The happy path (``verify_and_record_payment``) allocates explicitly to that
invoice. The reconciler used to pass ``allocations=None`` -- auto-allocate,
oldest invoice first -- so a customer who paid a newer invoice could have the
recovered payment applied to an *older* one instead: the bill they paid stays
open (inviting a second payment) and the ledger records them settling a bill they
never chose.

Reconciliation is a repair path. It must converge on the same outcome as the
happy path, not a different one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from app.models.billing import TopupIntent
from app.services.payment_reconciliation import (
    PaymentReconciliationError,
    _target_invoice_id,
)

_INVOICE_ID = "11111111-1111-1111-1111-111111111111"


def _intent(metadata: dict | None) -> TopupIntent:
    return TopupIntent(
        reference="ref-1",
        provider_type="paystack",
        status="pending",
        created_at=datetime.now(UTC) - timedelta(minutes=30),
        metadata_=metadata,
    )


def test_invoice_payment_targets_the_invoice_the_customer_chose():
    intent = _intent({"payment_flow": "invoice_payment", "invoice_id": _INVOICE_ID})

    assert _target_invoice_id(intent) == UUID(_INVOICE_ID)


def test_a_genuine_topup_has_no_explicit_invoice_target():
    assert _target_invoice_id(_intent({"payment_flow": "topup"})) is None
    assert _target_invoice_id(_intent(None)) is None
    assert _target_invoice_id(_intent({})) is None


def test_invoice_payment_without_an_invoice_id_does_not_guess():
    """An invoice checkout with no recorded invoice must fail closed."""
    intent = _intent({"payment_flow": "invoice_payment"})

    with pytest.raises(
        PaymentReconciliationError,
        match="no valid target invoice",
    ) as exc_info:
        _target_invoice_id(intent)

    assert exc_info.value.code.endswith("invoice_correlation_invalid")


def test_unparseable_invoice_id_does_not_guess():
    intent = _intent({"payment_flow": "invoice_payment", "invoice_id": "not-a-uuid"})

    with pytest.raises(PaymentReconciliationError) as exc_info:
        _target_invoice_id(intent)

    assert exc_info.value.code.endswith("invoice_correlation_invalid")
