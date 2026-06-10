"""Bank-transfer proof flow: upload -> staff verify -> wallet/invoice credit.

Verification creates a real Payment (status=succeeded, paid_at from the
claimed transfer date) through the standard billing service, optionally
auto-allocated to the account's oldest open invoices; anything unallocated
stays as account credit. Rejection records why. Both outcomes notify the
customer (and the submitting reseller user, when different) on push + email.
"""

from __future__ import annotations

import logging
import uuid as uuid_mod
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from fastapi import HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.models.billing import Invoice, InvoiceStatus, PaymentStatus
from app.models.payment_proof import PaymentProof, PaymentProofStatus
from app.services.common import apply_pagination, coerce_uuid, round_money, to_decimal

logger = logging.getLogger(__name__)

_UPLOAD_DIR = Path("uploads/payment_proofs")
_ALLOWED_SUFFIXES = {".jpg", ".jpeg", ".png", ".pdf", ".webp"}
_MAX_BYTES = 10 * 1024 * 1024


async def save_proof_file(file: UploadFile) -> str:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in _ALLOWED_SUFFIXES:
        raise HTTPException(
            status_code=400,
            detail="Upload a JPG, PNG, WEBP or PDF of the transfer receipt",
        )
    blob = await file.read()
    if len(blob) > _MAX_BYTES:
        raise HTTPException(status_code=400, detail="File too large (max 10 MB)")
    if not blob:
        raise HTTPException(status_code=400, detail="Empty file")
    _UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{uuid_mod.uuid4().hex}{suffix}"
    (_UPLOAD_DIR / name).write_bytes(blob)
    return str(_UPLOAD_DIR / name)


def _serialize(p: PaymentProof) -> dict:
    return {
        "id": str(p.id),
        "account_id": str(p.account_id),
        "amount": p.amount,
        "currency": p.currency,
        "bank_name": p.bank_name,
        "reference": p.reference,
        "paid_at": p.paid_at,
        "status": p.status.value,
        "review_notes": p.review_notes,
        "payment_id": str(p.payment_id) if p.payment_id else None,
        "created_at": p.created_at,
    }


def submit_proof(
    db: Session,
    account_id: str,
    *,
    submitted_by: str,
    amount,
    currency: str = "NGN",
    bank_name: str | None = None,
    reference: str | None = None,
    paid_at: datetime | None = None,
    file_path: str,
) -> dict:
    value = round_money(to_decimal(amount))
    if value <= Decimal("0.00"):
        raise HTTPException(status_code=400, detail="Amount must be greater than 0")
    proof = PaymentProof(
        account_id=coerce_uuid(account_id),
        submitted_by=coerce_uuid(submitted_by),
        amount=value,
        currency=(currency or "NGN")[:3].upper(),
        bank_name=(bank_name or "").strip() or None,
        reference=(reference or "").strip() or None,
        paid_at=paid_at,
        file_path=file_path,
    )
    db.add(proof)
    db.commit()
    db.refresh(proof)
    return _serialize(proof)


def list_for_account(
    db: Session, account_id: str, limit: int = 50, offset: int = 0
) -> list[dict]:
    query = (
        db.query(PaymentProof)
        .filter(PaymentProof.account_id == coerce_uuid(account_id))
        .order_by(PaymentProof.created_at.desc())
    )
    return [_serialize(p) for p in apply_pagination(query, limit, offset).all()]


def list_admin(
    db: Session, status: str | None = "submitted", limit: int = 100, offset: int = 0
) -> list[dict]:
    query = db.query(PaymentProof).order_by(PaymentProof.created_at.asc())
    if status:
        query = query.filter(PaymentProof.status == PaymentProofStatus(status))
    out = []
    for p in apply_pagination(query, limit, offset).all():
        d = _serialize(p)
        d["file_path"] = p.file_path
        d["submitted_by"] = str(p.submitted_by) if p.submitted_by else None
        out.append(d)
    return out


def _open_invoice_allocations(db: Session, account_id, amount: Decimal) -> list:
    """Spread the verified amount across the oldest open invoices."""
    from app.schemas.billing import PaymentAllocationApply

    open_statuses = {
        InvoiceStatus.issued,
        InvoiceStatus.partially_paid,
        InvoiceStatus.overdue,
    }
    invoices = (
        db.query(Invoice)
        .filter(Invoice.account_id == account_id)
        .filter(Invoice.is_active.is_(True))
        .filter(Invoice.status.in_(open_statuses))
        .order_by(Invoice.created_at.asc())
        .all()
    )
    allocations = []
    remaining = amount
    for inv in invoices:
        if remaining <= Decimal("0.00"):
            break
        due = Decimal(str(inv.balance_due or 0))
        if due <= Decimal("0.00"):
            continue
        take = min(due, remaining)
        allocations.append(
            PaymentAllocationApply(
                invoice_id=inv.id, amount=take, memo="bank-transfer proof"
            )
        )
        remaining -= take
    return allocations


def verify_proof(
    db: Session,
    proof_id: str,
    *,
    verified_by: str,
    auto_allocate: bool = True,
    review_notes: str | None = None,
) -> dict:
    proof = db.get(PaymentProof, coerce_uuid(proof_id))
    if proof is None:
        raise HTTPException(status_code=404, detail="Payment proof not found")
    if proof.status != PaymentProofStatus.submitted:
        raise HTTPException(status_code=400, detail="Proof already reviewed")

    from app.schemas.billing import PaymentCreate
    from app.services import billing as billing_service

    allocations = (
        _open_invoice_allocations(db, proof.account_id, Decimal(str(proof.amount)))
        if auto_allocate
        else None
    )
    payment = billing_service.payments.create(
        db,
        PaymentCreate(
            account_id=proof.account_id,
            amount=Decimal(str(proof.amount)),
            currency=proof.currency,
            status=PaymentStatus.succeeded,
            paid_at=proof.paid_at or datetime.now(UTC),
            external_id=(proof.reference or "")[:120] or None,
            memo=f"Bank transfer (proof {proof.id})",
            allocations=allocations or None,
        ),
    )
    proof.status = PaymentProofStatus.verified
    proof.verified_by = str(verified_by)
    proof.review_notes = (review_notes or "").strip() or None
    proof.payment_id = payment.id
    db.commit()
    db.refresh(proof)
    _notify(db, proof, approved=True)
    return _serialize(proof)


def reject_proof(
    db: Session, proof_id: str, *, verified_by: str, review_notes: str
) -> dict:
    proof = db.get(PaymentProof, coerce_uuid(proof_id))
    if proof is None:
        raise HTTPException(status_code=404, detail="Payment proof not found")
    if proof.status != PaymentProofStatus.submitted:
        raise HTTPException(status_code=400, detail="Proof already reviewed")
    if not (review_notes or "").strip():
        raise HTTPException(status_code=400, detail="A rejection reason is required")
    proof.status = PaymentProofStatus.rejected
    proof.verified_by = str(verified_by)
    proof.review_notes = review_notes.strip()
    db.commit()
    db.refresh(proof)
    _notify(db, proof, approved=False)
    return _serialize(proof)


def _notify(db: Session, proof: PaymentProof, *, approved: bool) -> None:
    """Best-effort push+email to the customer (and submitter when different)."""
    try:
        from app.models.notification import NotificationChannel
        from app.models.subscriber import Subscriber
        from app.schemas.notification import NotificationCreate
        from app.services.notification import notifications as notifications_svc

        targets = {proof.account_id}
        if proof.submitted_by and proof.submitted_by != proof.account_id:
            targets.add(proof.submitted_by)
        if approved:
            subject = "Bank transfer confirmed"
            body = (
                f"Your bank transfer of {proof.currency} {proof.amount} has been "
                "confirmed and credited to the account."
            )
        else:
            subject = "Bank transfer could not be confirmed"
            body = (
                f"We could not confirm the bank transfer of {proof.currency} "
                f"{proof.amount}."
            )
            if proof.review_notes:
                body += f"\n\nReason: {proof.review_notes}"
        for sid in targets:
            subscriber = db.get(Subscriber, sid)
            if not subscriber or not subscriber.email:
                continue
            for channel in (NotificationChannel.push, NotificationChannel.email):
                try:
                    notifications_svc.create(
                        db,
                        NotificationCreate(
                            channel=channel,
                            subscriber_id=sid,
                            recipient=subscriber.email,
                            subject=subject,
                            body=body,
                            category="billing",
                            event_type="payment_proof_"
                            + ("verified" if approved else "rejected"),
                        ),
                    )
                except Exception:
                    logger.warning("payment-proof notification failed", exc_info=True)
    except Exception:
        logger.warning("payment-proof notify block failed", exc_info=True)
