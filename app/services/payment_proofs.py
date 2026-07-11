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
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.models.billing import BillingAccount, Invoice, InvoiceStatus, PaymentStatus
from app.models.payment_proof import (
    PaymentProof,
    PaymentProofStatus,
    WithholdingTaxRecord,
    WithholdingTaxStatus,
)
from app.services.common import apply_pagination, coerce_uuid, round_money, to_decimal

logger = logging.getLogger(__name__)

_UPLOAD_DIR = Path("uploads/payment_proofs")
_ALLOWED_SUFFIXES = {".jpg", ".jpeg", ".png", ".pdf", ".webp"}
_MAX_BYTES = 10 * 1024 * 1024
_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
}


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
        "account_id": str(p.account_id) if p.account_id else None,
        "billing_account_id": str(p.billing_account_id)
        if p.billing_account_id
        else None,
        "amount": p.amount,
        "gross_amount": p.gross_amount,
        "wht_amount": p.wht_amount,
        "wht_rate": p.wht_rate,
        "verified_amount": p.verified_amount,
        "currency": p.currency,
        "bank_name": p.bank_name,
        "reference": p.reference,
        "paid_at": p.paid_at,
        "status": p.status.value,
        "review_notes": p.review_notes,
        "payment_id": str(p.payment_id) if p.payment_id else None,
        "created_at": p.created_at,
    }


def get_proof(db: Session, proof_id: str) -> PaymentProof | None:
    try:
        return db.get(PaymentProof, coerce_uuid(proof_id))
    except (ValueError, TypeError):
        return None


def find_duplicate_proofs(db: Session, proof: PaymentProof) -> list[PaymentProof]:
    """Other non-rejected proofs for the same subscriber with the same reference.

    Used to flag likely duplicate submissions of one receipt, and to block
    verifying a reference that already backs a verified proof's payment.
    """
    reference = (proof.reference or "").strip()
    if not reference:
        return []
    query = db.query(PaymentProof)
    # Scope to the same target — a subscriber account or a reseller billing
    # account — so a null account_id on consolidated proofs doesn't collide
    # across resellers.
    if proof.billing_account_id is not None:
        query = query.filter(
            PaymentProof.billing_account_id == proof.billing_account_id
        )
    else:
        query = query.filter(PaymentProof.account_id == proof.account_id)
    return (
        query.filter(PaymentProof.id != proof.id)
        .filter(PaymentProof.reference == reference)
        .filter(PaymentProof.status != PaymentProofStatus.rejected)
        .order_by(PaymentProof.created_at.asc())
        .all()
    )


def resolve_proof_file(proof: PaymentProof) -> tuple[Path, str]:
    """Return the (path, media_type) for a proof's receipt file.

    404s when the file is missing, and refuses any ``file_path`` that does not
    resolve under the payment-proofs upload directory (path traversal guard).
    """
    base = _UPLOAD_DIR.resolve()
    path = Path(proof.file_path).resolve()
    if not path.is_relative_to(base) or not path.is_file():
        raise HTTPException(status_code=404, detail="Receipt file not found")
    media_type = _MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")
    return path, media_type


def submit_proof(
    db: Session,
    account_id: str | None,
    *,
    submitted_by: str | None,
    amount,
    currency: str = "NGN",
    bank_name: str | None = None,
    reference: str | None = None,
    paid_at: datetime | None = None,
    file_path: str,
    billing_account_id: str | None = None,
    gross_amount=None,
    wht_rate=None,
) -> dict:
    """Record a transfer receipt.

    ``amount`` is the net cash transferred (what's on the receipt). For a
    reseller bulk transfer made net of withholding tax pass ``billing_account_id``
    plus ``gross_amount`` (the billed value) and/or ``wht_rate``; the withheld
    tax (gross − net) is stored so verification can credit the gross and raise a
    WHT receivable.
    """
    if not account_id and not billing_account_id:
        raise HTTPException(status_code=400, detail="A target account is required")
    value = round_money(to_decimal(amount))
    if value <= Decimal("0.00"):
        raise HTTPException(status_code=400, detail="Amount must be greater than 0")

    gross_value = round_money(to_decimal(gross_amount)) if gross_amount else None
    rate_value = to_decimal(wht_rate) if wht_rate not in (None, "") else None
    # Derive gross from rate when only a rate was supplied (net = gross·(1−rate)).
    if gross_value is None and rate_value and rate_value > 0:
        gross_value = round_money(value / (Decimal("1") - rate_value / Decimal("100")))
    wht_value = None
    if gross_value is not None:
        if gross_value < value:
            raise HTTPException(
                status_code=400,
                detail="Gross amount cannot be less than the transferred amount",
            )
        wht_value = round_money(gross_value - value)
        if rate_value is None and gross_value > 0:
            rate_value = round_money(wht_value / gross_value * Decimal("100"))

    proof = PaymentProof(
        account_id=coerce_uuid(account_id) if account_id else None,
        billing_account_id=(
            coerce_uuid(billing_account_id) if billing_account_id else None
        ),
        submitted_by=coerce_uuid(submitted_by) if submitted_by else None,
        amount=value,
        gross_amount=gross_value,
        wht_amount=wht_value,
        wht_rate=rate_value,
        currency=(currency or "NGN")[:3].upper(),
        bank_name=(bank_name or "").strip() or None,
        reference=(reference or "").strip() or None,
        paid_at=paid_at,
        file_path=file_path,
    )
    db.add(proof)
    db.commit()
    db.refresh(proof)
    out = _serialize(proof)
    # Flag likely duplicate submissions of the same receipt up front so both
    # the submitter and the reviewer see it before any money moves.
    out["duplicate_reference"] = bool(find_duplicate_proofs(db, proof))
    return out


def list_for_account(
    db: Session, account_id: str, limit: int = 50, offset: int = 0
) -> list[dict]:
    query = (
        db.query(PaymentProof)
        .filter(PaymentProof.account_id == coerce_uuid(account_id))
        .order_by(PaymentProof.created_at.desc())
    )
    return [_serialize(p) for p in apply_pagination(query, limit, offset).all()]


def list_for_billing_account(
    db: Session, billing_account_id: str, limit: int = 50, offset: int = 0
) -> list[dict]:
    """Consolidated (reseller) transfer proofs for one billing account."""
    query = (
        db.query(PaymentProof)
        .filter(PaymentProof.billing_account_id == coerce_uuid(billing_account_id))
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
    amount=None,
    auto_allocate: bool = True,
    review_notes: str | None = None,
    request=None,
) -> dict:
    """Confirm the transfer and create the real Payment.

    ``amount`` is the reviewer-confirmed amount from the bank statement; it
    defaults to the customer-claimed amount and is stored alongside it as
    ``verified_amount``. The subscriber row is locked (Postgres) and the
    status re-checked so two concurrent verifies cannot both create a payment.
    """
    proof = db.get(PaymentProof, coerce_uuid(proof_id))
    if proof is None:
        raise HTTPException(status_code=404, detail="Payment proof not found")
    if proof.status != PaymentProofStatus.submitted:
        raise HTTPException(status_code=400, detail="Proof already reviewed")

    # Reseller consolidated transfer (credits a billing account, may carry WHT).
    if proof.billing_account_id is not None:
        return _verify_consolidated_proof(
            db,
            proof,
            verified_by=verified_by,
            amount=amount,
            review_notes=review_notes,
            request=request,
        )

    from app.schemas.billing import PaymentCreate
    from app.services import billing as billing_service
    from app.services.billing._common import lock_account

    # Serialize concurrent reviews of this account's proofs, then re-check the
    # status: the loser of the race sees "already reviewed" instead of
    # double-creating the payment.
    lock_account(db, str(proof.account_id))
    db.refresh(proof)
    if proof.status != PaymentProofStatus.submitted:
        raise HTTPException(status_code=400, detail="Proof already reviewed")

    try:
        value = round_money(to_decimal(amount if amount is not None else proof.amount))
    except (InvalidOperation, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid verified amount") from exc
    if value <= Decimal("0.00"):
        raise HTTPException(
            status_code=400, detail="Verified amount must be greater than 0"
        )

    duplicates = find_duplicate_proofs(db, proof)
    verified_dup = next(
        (d for d in duplicates if d.status == PaymentProofStatus.verified), None
    )
    if verified_dup is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Reference '{proof.reference}' was already verified on another "
                f"proof ({verified_dup.id}) and paid. Reject this submission as "
                "a duplicate instead."
            ),
        )

    allocations = (
        _open_invoice_allocations(db, proof.account_id, value)
        if auto_allocate
        else None
    )
    payment = billing_service.payments.create(
        db,
        PaymentCreate(
            account_id=proof.account_id,
            amount=value,
            currency=proof.currency,
            status=PaymentStatus.succeeded,
            paid_at=proof.paid_at or datetime.now(UTC),
            external_id=(proof.reference or "")[:120] or None,
            memo=f"Bank transfer (proof {proof.id})",
            allocations=allocations or None,
        ),
        auto_allocate=auto_allocate,
    )
    proof.status = PaymentProofStatus.verified
    proof.verified_amount = value
    proof.verified_by = str(verified_by)
    proof.review_notes = (review_notes or "").strip() or None
    proof.payment_id = payment.id
    db.commit()
    db.refresh(proof)
    _audit(
        db,
        request,
        action="verify",
        proof=proof,
        actor_id=verified_by,
        metadata={
            "claimed_amount": str(proof.amount),
            "verified_amount": str(value),
            "currency": proof.currency,
            "reference": proof.reference,
            "payment_id": str(payment.id),
            "auto_allocate": auto_allocate,
        },
    )
    _notify(db, proof, approved=True)
    return _serialize(proof)


def _verify_consolidated_proof(
    db: Session,
    proof: PaymentProof,
    *,
    verified_by: str,
    amount=None,
    review_notes: str | None = None,
    request=None,
) -> dict:
    """Verify a reseller bulk transfer: credit the billing account the gross and
    raise a withholding-tax receivable for any tax withheld at source.

    ``amount`` (reviewer-confirmed) is the *net cash* received; the account is
    credited ``net + wht`` so the withheld tax stays a tracked, reclaimable
    receivable rather than vanishing from the reseller's balance."""
    from app.schemas.billing import PaymentCreate
    from app.services import billing as billing_service

    ba = db.get(BillingAccount, proof.billing_account_id)
    if ba is None:
        raise HTTPException(status_code=404, detail="Billing account not found")

    try:
        net_value = round_money(
            to_decimal(amount if amount is not None else proof.amount)
        )
    except (InvalidOperation, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid verified amount") from exc
    if net_value <= Decimal("0.00"):
        raise HTTPException(
            status_code=400, detail="Verified amount must be greater than 0"
        )

    wht_value = (
        round_money(to_decimal(proof.wht_amount))
        if proof.wht_amount
        else Decimal("0.00")
    )
    gross_value = round_money(net_value + wht_value)

    verified_dup = next(
        (
            d
            for d in find_duplicate_proofs(db, proof)
            if d.status == PaymentProofStatus.verified
        ),
        None,
    )
    if verified_dup is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Reference '{proof.reference}' was already verified on another "
                f"proof ({verified_dup.id}). Reject this submission as a duplicate."
            ),
        )

    payment = billing_service.payments.create(
        db,
        PaymentCreate(
            billing_account_id=ba.id,
            amount=gross_value,
            currency=proof.currency,
            status=PaymentStatus.succeeded,
            paid_at=proof.paid_at or datetime.now(UTC),
            external_id=(proof.reference or "")[:120] or None,
            memo=f"Reseller bank transfer (proof {proof.id})",
            allocations=None,
        ),
        auto_allocate=False,
    )
    proof.status = PaymentProofStatus.verified
    proof.verified_amount = net_value
    proof.verified_by = str(verified_by)
    proof.review_notes = (review_notes or "").strip() or None
    proof.payment_id = payment.id

    wht_record_id = None
    if wht_value > Decimal("0.00"):
        record = WithholdingTaxRecord(
            billing_account_id=ba.id,
            reseller_id=ba.reseller_id,
            payment_id=payment.id,
            payment_proof_id=proof.id,
            gross_amount=gross_value,
            net_amount=net_value,
            wht_amount=wht_value,
            wht_rate=proof.wht_rate,
            currency=proof.currency,
            status=WithholdingTaxStatus.pending,
        )
        db.add(record)
        db.flush()
        wht_record_id = str(record.id)

    db.commit()
    db.refresh(proof)
    _audit(
        db,
        request,
        action="verify",
        proof=proof,
        actor_id=verified_by,
        metadata={
            "billing_account_id": str(ba.id),
            "net_amount": str(net_value),
            "gross_amount": str(gross_value),
            "wht_amount": str(wht_value),
            "currency": proof.currency,
            "reference": proof.reference,
            "payment_id": str(payment.id),
            "withholding_tax_record_id": wht_record_id,
        },
    )
    # _notify targets account_id (None here) + submitted_by, so the reseller
    # user who uploaded the receipt is notified; the null account is skipped.
    _notify(db, proof, approved=True)
    out = _serialize(proof)
    out["withholding_tax_record_id"] = wht_record_id
    return out


def list_withholding_tax_records(
    db: Session,
    *,
    billing_account_id: str | None = None,
    reseller_id: str | None = None,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """Withholding-tax receivables, newest first, for admin/reseller views."""
    query = db.query(WithholdingTaxRecord).order_by(
        WithholdingTaxRecord.created_at.desc()
    )
    if billing_account_id:
        query = query.filter(
            WithholdingTaxRecord.billing_account_id == coerce_uuid(billing_account_id)
        )
    if reseller_id:
        query = query.filter(
            WithholdingTaxRecord.reseller_id == coerce_uuid(reseller_id)
        )
    if status:
        query = query.filter(
            WithholdingTaxRecord.status == WithholdingTaxStatus(status)
        )
    return [
        {
            "id": str(r.id),
            "billing_account_id": str(r.billing_account_id),
            "reseller_id": str(r.reseller_id) if r.reseller_id else None,
            "payment_id": str(r.payment_id) if r.payment_id else None,
            "gross_amount": r.gross_amount,
            "net_amount": r.net_amount,
            "wht_amount": r.wht_amount,
            "wht_rate": r.wht_rate,
            "currency": r.currency,
            "status": r.status.value,
            "created_at": r.created_at,
        }
        for r in apply_pagination(query, limit, offset).all()
    ]


def reject_proof(
    db: Session,
    proof_id: str,
    *,
    verified_by: str,
    review_notes: str,
    request=None,
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
    _audit(
        db,
        request,
        action="reject",
        proof=proof,
        actor_id=verified_by,
        metadata={
            "claimed_amount": str(proof.amount),
            "currency": proof.currency,
            "reference": proof.reference,
            "reason": proof.review_notes,
        },
    )
    _notify(db, proof, approved=False)
    return _serialize(proof)


def _audit(
    db: Session,
    request,
    *,
    action: str,
    proof: PaymentProof,
    actor_id: str | None,
    metadata: dict,
) -> None:
    """Best-effort audit trail for proof reviews (money-moving admin action)."""
    try:
        from app.services.audit_helpers import log_audit_event

        log_audit_event(
            db=db,
            request=request,
            action=action,
            entity_type="payment_proof",
            entity_id=str(proof.id),
            actor_id=str(actor_id) if actor_id else None,
            metadata=metadata,
        )
    except Exception:
        logger.warning("payment-proof audit event failed", exc_info=True)


def _notify(db: Session, proof: PaymentProof, *, approved: bool) -> None:
    """Best-effort push+email to the customer (and submitter when different)."""
    try:
        from app.models.notification import NotificationChannel
        from app.models.subscriber import Subscriber
        from app.schemas.notification import NotificationCreate
        from app.services.notification import notifications as notifications_svc

        # account_id is null for reseller consolidated proofs; notify the
        # submitting reseller user in that case. Drop null ids so we never
        # db.get(Subscriber, None).
        targets = {proof.account_id}
        if proof.submitted_by and proof.submitted_by != proof.account_id:
            targets.add(proof.submitted_by)
        targets.discard(None)
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
                    notifications_svc.create_customer_notification(
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
