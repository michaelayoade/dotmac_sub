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
from decimal import Decimal

from app.models.billing import TopupIntent
from app.services.payment_reconciliation import _intent_allocations

_AMOUNT = Decimal("5000.00")
_INVOICE_ID = "11111111-1111-1111-1111-111111111111"


def _intent(metadata: dict | None) -> TopupIntent:
    return TopupIntent(
        reference="ref-1",
        provider_type="paystack",
        status="pending",
        created_at=datetime.now(UTC) - timedelta(minutes=30),
        metadata_=metadata,
    )


def test_invoice_payment_allocates_to_the_invoice_the_customer_chose():
    intent = _intent(
        {"payment_flow": "invoice_payment", "invoice_id": _INVOICE_ID}
    )

    allocations = _intent_allocations(intent, _AMOUNT)

    assert allocations is not None
    assert len(allocations) == 1
    assert str(allocations[0].invoice_id) == _INVOICE_ID
    assert allocations[0].amount == _AMOUNT


def test_a_genuine_topup_still_auto_allocates():
    """Top-ups have no target invoice -- oldest-first auto-allocation is correct
    for them, and must not regress."""
    assert _intent_allocations(_intent({"payment_flow": "topup"}), _AMOUNT) is None
    assert _intent_allocations(_intent(None), _AMOUNT) is None
    assert _intent_allocations(_intent({}), _AMOUNT) is None


def test_invoice_payment_without_an_invoice_id_does_not_guess():
    """An invoice checkout with no recorded invoice is an upstream bug. Fall back
    to auto-allocation rather than silently picking a bill to settle."""
    intent = _intent({"payment_flow": "invoice_payment"})

    assert _intent_allocations(intent, _AMOUNT) is None


def test_unparseable_invoice_id_does_not_guess():
    intent = _intent(
        {"payment_flow": "invoice_payment", "invoice_id": "not-a-uuid"}
    )

    assert _intent_allocations(intent, _AMOUNT) is None
