from __future__ import annotations

from decimal import Decimal

from app.models.billing import Payment, PaymentStatus
from app.services import web_billing_payments


def _accepted_payment(db_session, subscriber) -> Payment:
    payment = Payment(
        account_id=subscriber.id,
        amount=Decimal("2500.00"),
        currency="NGN",
        status=PaymentStatus.succeeded,
    )
    db_session.add(payment)
    db_session.commit()
    db_session.refresh(payment)
    return payment


def test_payment_correction_action_requires_update_permission(db_session, subscriber):
    payment = _accepted_payment(db_session, subscriber)

    state = web_billing_payments.build_payment_detail_data(
        db_session,
        payment_id=str(payment.id),
        can_update_payment=False,
    )

    assert state is not None
    action = state["payment_correction_action"]
    assert isinstance(action, web_billing_payments.PaymentCorrectionAction)
    assert action.allowed is False
    assert action.permission == "billing:payment:update"


def test_payment_correction_action_uses_reversal_preview_when_allowed(
    db_session, subscriber
):
    payment = _accepted_payment(db_session, subscriber)

    state = web_billing_payments.build_payment_detail_data(
        db_session,
        payment_id=str(payment.id),
        can_update_payment=True,
    )

    assert state is not None
    action = state["payment_correction_action"]
    assert isinstance(action, web_billing_payments.PaymentCorrectionAction)
    assert action.allowed is True
    assert action.label == "Correct accepted payment"
    assert action.preview_url.endswith(f"/payments/{payment.id}/reversal/preview")
