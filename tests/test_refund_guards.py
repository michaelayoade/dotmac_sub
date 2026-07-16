"""Refund amount guards (review #B / refund double-spend hardening).

The refund row is now locked before computing the refundable amount (serializes
concurrent refunds), and the amount math rejects non-positive / over-refund.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.billing import Payment, PaymentStatus
from app.services.billing.payments import Refunds


def _refund(db, payment_id, amount):
    return Refunds.process_refund(
        db,
        str(payment_id),
        refund_amount=amount,
        idempotency_key=f"refund-test-{uuid4().hex}",
    )


def _succeeded_payment(db, subscriber, ext, amount="100.00"):
    p = Payment(
        account_id=subscriber.id,
        amount=Decimal(amount),
        status=PaymentStatus.succeeded,
        external_id=ext,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def test_refund_rejects_non_positive(db_session, subscriber):
    p = _succeeded_payment(db_session, subscriber, "RF-1")
    with pytest.raises(HTTPException) as e:
        _refund(db_session, p.id, Decimal("0"))
    assert e.value.status_code == 400


def test_refund_rejects_over_refund(db_session, subscriber):
    p = _succeeded_payment(db_session, subscriber, "RF-2")
    with pytest.raises(HTTPException) as e:
        _refund(db_session, p.id, Decimal("150.00"))
    assert e.value.status_code == 400


def test_partial_then_full_refund_accounting(db_session, subscriber):
    p = _succeeded_payment(db_session, subscriber, "RF-3")
    _refund(db_session, p.id, Decimal("40.00"))
    db_session.refresh(p)
    assert p.status == PaymentStatus.partially_refunded

    # Refund the remaining 60 → fully refunded.
    _refund(db_session, p.id, Decimal("60.00"))
    db_session.refresh(p)
    assert p.status == PaymentStatus.refunded

    # A further refund is rejected (nothing left).
    with pytest.raises(HTTPException) as e:
        _refund(db_session, p.id, Decimal("1.00"))
    assert e.value.status_code == 409


def test_refunded_amount_tracks_running_total(db_session, subscriber):
    """Gross `amount` is unchanged; refunded_amount accumulates each refund."""
    p = _succeeded_payment(db_session, subscriber, "RF-4")
    assert p.refunded_amount == Decimal("0.00")

    _refund(db_session, p.id, Decimal("30.00"))
    db_session.refresh(p)
    assert p.amount == Decimal("100.00")  # gross captured figure unchanged
    assert p.refunded_amount == Decimal("30.00")
    assert p.status == PaymentStatus.partially_refunded

    _refund(db_session, p.id, Decimal("70.00"))
    db_session.refresh(p)
    assert p.refunded_amount == Decimal("100.00")
    assert p.status == PaymentStatus.refunded


def test_payment_read_exposes_refunded_amount(db_session, subscriber):
    """The ERP-facing PaymentRead surfaces refunded_amount (net = amount - it)."""
    from app.schemas.billing import PaymentRead

    p = _succeeded_payment(db_session, subscriber, "RF-5")
    _refund(db_session, p.id, Decimal("25.00"))
    db_session.refresh(p)
    read = PaymentRead.model_validate(p)
    assert read.refunded_amount == Decimal("25.00")
