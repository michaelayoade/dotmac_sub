"""Bank-transfer proof flow: submit -> verify/reject -> credit + notify."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.models.billing import Payment, PaymentStatus
from app.models.notification import Notification
from app.services import payment_proofs as svc


def _account(db_session):
    from app.models.subscriber import Subscriber

    sub = Subscriber(
        first_name="Pay",
        last_name="Transfer",
        email="pay.transfer@example.com",
    )
    db_session.add(sub)
    db_session.commit()
    return sub


def test_submit_validates_amount(db_session):
    sub = _account(db_session)
    with pytest.raises(HTTPException) as exc:
        svc.submit_proof(
            db_session,
            str(sub.id),
            submitted_by=str(sub.id),
            amount="0",
            file_path="uploads/payment_proofs/x.png",
        )
    assert exc.value.status_code == 400


def test_verify_creates_succeeded_payment_and_notifies(db_session):
    sub = _account(db_session)
    proof = svc.submit_proof(
        db_session,
        str(sub.id),
        submitted_by=str(sub.id),
        amount="5000",
        bank_name="GTB",
        reference="TRF-123",
        paid_at=datetime(2026, 6, 9, tzinfo=UTC),
        file_path="uploads/payment_proofs/x.png",
    )
    assert proof["status"] == "submitted"

    out = svc.verify_proof(
        db_session, proof["id"], verified_by="admin-1", auto_allocate=True
    )
    assert out["status"] == "verified"
    assert out["payment_id"] is not None

    payment = db_session.get(Payment, out["payment_id"])
    assert payment.status == PaymentStatus.succeeded
    assert Decimal(str(payment.amount)) == Decimal("5000.00")
    assert "TRF-123" == payment.external_id

    notes = (
        db_session.query(Notification)
        .filter(Notification.subscriber_id == sub.id)
        .filter(Notification.event_type == "payment_proof_verified")
        .all()
    )
    assert len(notes) == 2  # push + email

    # Double review is rejected.
    with pytest.raises(HTTPException) as exc:
        svc.verify_proof(db_session, proof["id"], verified_by="admin-1")
    assert exc.value.status_code == 400


def test_reject_requires_reason_and_notifies(db_session):
    sub = _account(db_session)
    proof = svc.submit_proof(
        db_session,
        str(sub.id),
        submitted_by=str(sub.id),
        amount="2500",
        file_path="uploads/payment_proofs/y.png",
    )
    with pytest.raises(HTTPException):
        svc.reject_proof(
            db_session, proof["id"], verified_by="admin-1", review_notes="  "
        )
    out = svc.reject_proof(
        db_session,
        proof["id"],
        verified_by="admin-1",
        review_notes="Amount does not match our statement",
    )
    assert out["status"] == "rejected"
    notes = (
        db_session.query(Notification)
        .filter(Notification.event_type == "payment_proof_rejected")
        .all()
    )
    assert len(notes) == 2
    assert "statement" in notes[0].body
