"""Bank-transfer proof flow: submit -> verify/reject -> credit + notify."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.models.billing import (
    Invoice,
    InvoiceStatus,
    Payment,
    PaymentAllocation,
    PaymentStatus,
)
from app.models.notification import Notification
from app.models.subscriber import SubscriberStatus
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


def _open_invoice(db_session, sub, amount="3000.00"):
    invoice = Invoice(
        account_id=sub.id,
        status=InvoiceStatus.issued,
        subtotal=Decimal(amount),
        tax_total=Decimal("0.00"),
        total=Decimal(amount),
        balance_due=Decimal(amount),
    )
    db_session.add(invoice)
    db_session.commit()
    db_session.refresh(invoice)
    return invoice


def _submit(db_session, sub, amount="5000", reference=None, file_path="x.png"):
    return svc.submit_proof(
        db_session,
        str(sub.id),
        submitted_by=str(sub.id),
        amount=amount,
        reference=reference,
        file_path=f"uploads/payment_proofs/{file_path}",
    )


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


def test_verify_resolves_delinquent_status_after_paid_invoice(db_session):
    from app.models.catalog import (
        AccessType,
        BillingMode,
        CatalogOffer,
        OfferStatus,
        PriceBasis,
        ServiceType,
        Subscription,
        SubscriptionStatus,
    )
    from app.models.collections import DunningCase, DunningCaseStatus

    sub = _account(db_session)
    sub.status = SubscriberStatus.delinquent
    offer = CatalogOffer(
        name="Proof Restore Plan",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        status=OfferStatus.active,
        is_active=True,
    )
    db_session.add(offer)
    db_session.flush()
    db_session.add(
        Subscription(
            subscriber_id=sub.id,
            offer_id=offer.id,
            status=SubscriptionStatus.active,
            billing_mode=BillingMode.prepaid,
        )
    )
    invoice = _open_invoice(db_session, sub, "3000.00")
    db_session.add(DunningCase(account_id=sub.id, status=DunningCaseStatus.open))
    db_session.commit()

    proof = _submit(db_session, sub, amount="3000", reference="TRF-RESTORE")
    svc.verify_proof(db_session, proof["id"], verified_by="admin-1")

    db_session.refresh(sub)
    db_session.refresh(invoice)
    assert invoice.status == InvoiceStatus.paid
    assert invoice.balance_due == Decimal("0.00")
    assert sub.status == SubscriberStatus.active
    assert (
        db_session.query(DunningCase)
        .filter(DunningCase.account_id == sub.id)
        .one()
        .status
        == DunningCaseStatus.resolved
    )


def test_reject_requires_reason_and_notifies(db_session):
    sub = _account(db_session)
    proof = svc.submit_proof(
        db_session,
        str(sub.id),
        submitted_by=str(sub.id),
        amount="2500",
        file_path="uploads/payment_proofs/y.png",
    )
    with pytest.raises(svc.PaymentProofReviewError) as exc:
        svc.reject_proof(
            db_session, proof["id"], verified_by="admin-1", review_notes="  "
        )
    assert exc.value.code == "rejection_reason_required"
    assert exc.value.field == "review_notes"
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


def test_verify_with_admin_amount_override(db_session):
    """The Payment uses the reviewer-confirmed amount, stored alongside the
    customer-claimed amount."""
    sub = _account(db_session)
    proof = _submit(db_session, sub, amount="5000", reference="TRF-OVR")

    out = svc.verify_proof(
        db_session, proof["id"], verified_by="admin-1", amount="4500"
    )
    assert Decimal(str(out["amount"])) == Decimal("5000.00")  # claimed, unchanged
    assert Decimal(str(out["verified_amount"])) == Decimal("4500.00")

    payment = db_session.get(Payment, out["payment_id"])
    assert Decimal(str(payment.amount)) == Decimal("4500.00")


def test_verify_rejects_invalid_or_nonpositive_amount(db_session):
    sub = _account(db_session)
    proof = _submit(db_session, sub)
    with pytest.raises(HTTPException) as exc:
        svc.verify_proof(db_session, proof["id"], verified_by="admin-1", amount="0")
    assert exc.value.status_code == 400
    assert isinstance(exc.value, svc.PaymentProofReviewError)
    assert exc.value.code == "verified_amount_non_positive"
    assert exc.value.field == "amount"
    with pytest.raises(svc.PaymentProofReviewError) as exc:
        svc.verify_proof(
            db_session, proof["id"], verified_by="admin-1", amount="not-a-number"
        )
    assert exc.value.status_code == 400
    assert exc.value.code == "invalid_verified_amount"
    assert exc.value.field == "amount"


def test_verify_without_auto_allocate_keeps_money_as_credit(db_session):
    """auto_allocate=False must NOT silently fall back to auto-allocation:
    the open invoice stays open and the amount becomes account credit."""
    from app.services.billing._common import get_account_credit_balance

    sub = _account(db_session)
    invoice = _open_invoice(db_session, sub, amount="3000.00")
    proof = _submit(db_session, sub, amount="5000", reference="TRF-CRED")

    out = svc.verify_proof(
        db_session, proof["id"], verified_by="admin-1", auto_allocate=False
    )
    payment = db_session.get(Payment, out["payment_id"])
    allocations = (
        db_session.query(PaymentAllocation)
        .filter(PaymentAllocation.payment_id == payment.id)
        .all()
    )
    assert allocations == []
    db_session.refresh(invoice)
    assert invoice.status == InvoiceStatus.issued
    assert Decimal(str(invoice.balance_due)) == Decimal("3000.00")
    assert get_account_credit_balance(db_session, str(sub.id)) == Decimal("5000.00")


def test_verify_with_auto_allocate_pays_open_invoice(db_session):
    sub = _account(db_session)
    invoice = _open_invoice(db_session, sub, amount="3000.00")
    proof = _submit(db_session, sub, amount="5000", reference="TRF-ALLOC")

    out = svc.verify_proof(
        db_session, proof["id"], verified_by="admin-1", auto_allocate=True
    )
    db_session.refresh(invoice)
    assert invoice.status == InvoiceStatus.paid
    payment = db_session.get(Payment, out["payment_id"])
    allocations = (
        db_session.query(PaymentAllocation)
        .filter(PaymentAllocation.payment_id == payment.id)
        .all()
    )
    assert len(allocations) == 1
    assert Decimal(str(allocations[0].amount)) == Decimal("3000.00")


def test_duplicate_reference_is_flagged_on_submit(db_session):
    sub = _account(db_session)
    first = _submit(db_session, sub, reference="TRF-DUP", file_path="a.png")
    assert first["duplicate_reference"] is False
    second = _submit(db_session, sub, reference="TRF-DUP", file_path="b.png")
    assert second["duplicate_reference"] is True
    # A different reference is not flagged.
    other = _submit(db_session, sub, reference="TRF-OTHER", file_path="c.png")
    assert other["duplicate_reference"] is False


def test_verify_blocked_when_reference_already_verified(db_session):
    """The same receipt reference cannot be paid out twice."""
    sub = _account(db_session)
    first = _submit(db_session, sub, reference="TRF-TWICE", file_path="a.png")
    second = _submit(db_session, sub, reference="TRF-TWICE", file_path="b.png")

    svc.verify_proof(db_session, first["id"], verified_by="admin-1")
    with pytest.raises(HTTPException) as exc:
        svc.verify_proof(db_session, second["id"], verified_by="admin-1")
    assert exc.value.status_code == 409
    assert "TRF-TWICE" in exc.value.detail
    assert isinstance(exc.value, svc.PaymentProofReviewError)
    assert exc.value.code == "duplicate_transfer_reference"

    # Only one payment was created for that reference.
    payments = (
        db_session.query(Payment).filter(Payment.external_id == "TRF-TWICE").all()
    )
    assert len(payments) == 1
    # The duplicate can still be rejected.
    out = svc.reject_proof(
        db_session, second["id"], verified_by="admin-1", review_notes="Duplicate"
    )
    assert out["status"] == "rejected"


def test_verify_and_reject_emit_audit_events(db_session):
    from app.models.audit import AuditEvent

    sub = _account(db_session)
    proof = _submit(db_session, sub, reference="TRF-AUD", file_path="a.png")
    svc.verify_proof(db_session, proof["id"], verified_by="admin-1", amount="4999.50")
    other = _submit(db_session, sub, reference="TRF-AUD-2", file_path="b.png")
    svc.reject_proof(
        db_session, other["id"], verified_by="admin-2", review_notes="No match"
    )

    events = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.entity_type == "payment_proof")
        .all()
    )
    by_action = {e.action: e for e in events}
    assert set(by_action) == {"verify", "reject"}
    verify_meta = by_action["verify"].metadata_ or {}
    assert verify_meta.get("verified_amount") == "4999.50"
    assert verify_meta.get("claimed_amount") == "5000.00"
    reject_meta = by_action["reject"].metadata_ or {}
    assert reject_meta.get("reason") == "No match"
