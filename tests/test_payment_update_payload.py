"""Payment edit must not send the immutable account_id.

Regression: build_update_payload always set account_id, and
billing_service.payments.update() rejects any payload containing it
("Payment scope ... cannot be changed after creation"). Because the edit form
always submits the locked account, every payment edit (status change, memo,
etc.) failed with a 400.
"""

from decimal import Decimal

import pytest

from app.services.web_billing_payments import build_update_payload


@pytest.mark.parametrize("status", ["refunded", "partially_refunded", "reversed"])
def test_update_payload_omits_account_id_and_owner_evidence_status(status):
    payload = build_update_payload(
        payment_method_id=None,
        amount=Decimal("1075.00"),
        currency="NGN",
        status=status,
        memo=None,
    )
    data = payload.model_dump(exclude_unset=True)
    assert "account_id" not in data
    assert "billing_account_id" not in data
    # Refund/reversal status is owned by exact evidence, not the generic editor.
    assert "status" not in data
    assert set(data) == {"memo"}
