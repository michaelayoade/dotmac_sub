"""Payment mark_status transition guard (review #A4).

Out-of-order / replayed gateway webhooks must not regress committed financial
state: a late success after refund, or a late failure after success, is a
no-op (the webhook still gets its payment back / 200s).
"""

from __future__ import annotations

from decimal import Decimal

from app.models.billing import Payment, PaymentStatus
from app.services import billing as billing_service


def _payment(db, subscriber, status, ext):
    p = Payment(
        account_id=subscriber.id,
        amount=Decimal("100.00"),
        status=status,
        external_id=ext,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def test_succeeded_to_failed_is_ignored(db_session, subscriber):
    """Late gateway 'failed' after success must not un-settle the payment."""
    p = _payment(db_session, subscriber, PaymentStatus.succeeded, "TRX-A4-1")
    result = billing_service.payments.mark_status(
        db_session, str(p.id), PaymentStatus.failed
    )
    assert result.status == PaymentStatus.succeeded  # unchanged


def test_refunded_to_succeeded_is_ignored(db_session, subscriber):
    """Replayed 'success' after a refund must not resurrect the payment."""
    p = _payment(db_session, subscriber, PaymentStatus.refunded, "TRX-A4-2")
    result = billing_service.payments.mark_status(
        db_session, str(p.id), PaymentStatus.succeeded
    )
    assert result.status == PaymentStatus.refunded  # unchanged


def test_pending_to_succeeded_is_allowed(db_session, subscriber):
    """Control: the legal settlement transition still works."""
    p = _payment(db_session, subscriber, PaymentStatus.pending, "TRX-A4-3")
    result = billing_service.payments.mark_status(
        db_session, str(p.id), PaymentStatus.succeeded
    )
    assert result.status == PaymentStatus.succeeded
    assert result.paid_at is not None
