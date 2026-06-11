"""Service helpers for the bank-transfer payment-proof admin web pages."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from app.models.payment_proof import PaymentProof, PaymentProofStatus
from app.services import payment_proofs as payment_proofs_service

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def list_data(
    db: Session,
    *,
    status: str | None,
    page: int,
    per_page: int,
) -> dict[str, object]:
    """Build template context for the payment proofs list page (newest first)."""
    query = db.query(PaymentProof)
    if status:
        try:
            query = query.filter(PaymentProof.status == PaymentProofStatus(status))
        except ValueError:
            status = None
    total = query.count()
    proofs = (
        query.order_by(PaymentProof.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    submitted_count = (
        db.query(PaymentProof)
        .filter(PaymentProof.status == PaymentProofStatus.submitted)
        .count()
    )
    total_pages = (total + per_page - 1) // per_page if total else 1

    return {
        "proofs": proofs,
        "statuses": [s.value for s in PaymentProofStatus],
        "status_filter": status,
        "submitted_count": submitted_count,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
    }


def detail_data(db: Session, *, proof_id: str) -> dict[str, object] | None:
    """Build template context for the payment proof detail page."""
    proof = payment_proofs_service.get_proof(db, proof_id)
    if not proof:
        return None

    duplicates = payment_proofs_service.find_duplicate_proofs(db, proof)
    suffix = Path(proof.file_path or "").suffix.lower()
    try:
        payment_proofs_service.resolve_proof_file(proof)
        file_available = True
    except Exception:
        file_available = False

    return {
        "proof": proof,
        "account": proof.account,
        "payment": proof.payment,
        "duplicates": duplicates,
        "file_available": file_available,
        "file_is_image": suffix in _IMAGE_SUFFIXES,
        "file_is_pdf": suffix == ".pdf",
    }


def file_response_args(db: Session, *, proof_id: str) -> tuple[Path, str] | None:
    """Resolve (path, media_type) for streaming a proof's receipt, or None."""
    proof = payment_proofs_service.get_proof(db, proof_id)
    if not proof:
        return None
    return payment_proofs_service.resolve_proof_file(proof)


def verify_proof(
    db: Session,
    request,
    *,
    proof_id: str,
    verified_by: str,
    amount: str | None,
    auto_allocate: bool,
    review_notes: str | None,
) -> dict:
    return payment_proofs_service.verify_proof(
        db,
        proof_id,
        verified_by=verified_by,
        amount=(amount or "").strip() or None,
        auto_allocate=auto_allocate,
        review_notes=review_notes,
        request=request,
    )


def reject_proof(
    db: Session,
    request,
    *,
    proof_id: str,
    verified_by: str,
    review_notes: str,
) -> dict:
    return payment_proofs_service.reject_proof(
        db,
        proof_id,
        verified_by=verified_by,
        review_notes=review_notes,
        request=request,
    )
