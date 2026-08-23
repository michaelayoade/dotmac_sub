"""Web application coordinator for legacy VAT reconciliation."""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.billing import TaxApplication
from app.schemas.billing import CreditNoteIssuePreviewRequest, CreditNoteIssueRequest
from app.services.billing.credit_notes import (
    CreditIssuePreview,
    CreditIssueResult,
    CreditNotes,
)
from app.services.billing_tax_reconciliation import (
    TaxReconciliationCandidate,
    get_tax_reconciliation_candidate,
    list_tax_reconciliation_candidates,
)
from app.services.domain_errors import DomainError


class TaxReconciliationCreditError(DomainError):
    """Transport-neutral failure while preparing a VAT correction credit."""

    def __init__(self, *, suffix: str, message: str) -> None:
        super().__init__(
            code=f"financial.billing_tax_reconciliation.{suffix}",
            message=message,
            details={},
        )


@dataclass(frozen=True)
class TaxCreditReview:
    candidate: TaxReconciliationCandidate
    payload: CreditNoteIssuePreviewRequest
    preview: CreditIssuePreview
    idempotency_key: str


def build_tax_reconciliation_data(
    db: Session,
    *,
    page: int,
    per_page: int,
) -> dict[str, object]:
    bounded_page = max(1, page)
    result = list_tax_reconciliation_candidates(
        db,
        offset=(bounded_page - 1) * per_page,
        limit=per_page,
    )
    return {
        "candidates": result.candidates,
        "page": bounded_page,
        "per_page": result.limit,
        "has_previous": bounded_page > 1,
        "has_more": result.has_more,
    }


def _candidate_or_conflict(
    db: Session,
    *,
    invoice_id: UUID,
    candidate_fingerprint: str,
) -> TaxReconciliationCandidate:
    candidate = get_tax_reconciliation_candidate(db, invoice_id)
    if candidate is None:
        raise TaxReconciliationCreditError(
            suffix="candidate_resolved",
            message=(
                "This invoice is no longer an unresolved tax-reconciliation "
                "candidate. Refresh the queue before taking action."
            ),
        )
    if not hmac.compare_digest(candidate.fingerprint, candidate_fingerprint):
        raise TaxReconciliationCreditError(
            suffix="stale_candidate",
            message="Tax evidence changed after review; refresh the queue",
        )
    if not candidate.can_prepare_tax_credit:
        raise TaxReconciliationCreditError(
            suffix="unproven_exact_correction",
            message=(
                "The available evidence does not prove an exact tax correction. "
                "Review the invoice and customer evidence manually."
            ),
        )
    return candidate


def _credit_payload(
    candidate: TaxReconciliationCandidate,
) -> CreditNoteIssuePreviewRequest:
    reference = candidate.invoice_number or str(candidate.invoice_id)
    marker = f"[tax-reconciliation:{candidate.fingerprint}]"
    return CreditNoteIssuePreviewRequest(
        account_id=candidate.account_id,
        invoice_id=candidate.invoice_id,
        currency=candidate.currency,
        subtotal=Decimal("0.00"),
        tax_total=candidate.maximum_remaining_adjustment,
        total=candidate.maximum_remaining_adjustment,
        memo=f"VAT exemption correction for invoice {reference} {marker}",
        line_description=None,
        line_tax_rate_id=None,
        line_tax_application=TaxApplication.exempt,
    )


def prepare_tax_credit_review(
    db: Session,
    *,
    invoice_id: UUID,
    candidate_fingerprint: str,
) -> TaxCreditReview:
    candidate = _candidate_or_conflict(
        db,
        invoice_id=invoice_id,
        candidate_fingerprint=candidate_fingerprint,
    )
    payload = _credit_payload(candidate)
    return TaxCreditReview(
        candidate=candidate,
        payload=payload,
        preview=CreditNotes.preview_issue(db, payload),
        idempotency_key=secrets.token_urlsafe(24),
    )


def issue_tax_credit(
    db: Session,
    *,
    invoice_id: UUID,
    candidate_fingerprint: str,
    preview_fingerprint: str,
    idempotency_key: str,
) -> CreditIssueResult:
    candidate = _candidate_or_conflict(
        db,
        invoice_id=invoice_id,
        candidate_fingerprint=candidate_fingerprint,
    )
    payload = _credit_payload(candidate)
    current_preview = CreditNotes.preview_issue(db, payload)
    if not hmac.compare_digest(current_preview.fingerprint, preview_fingerprint):
        raise TaxReconciliationCreditError(
            suffix="stale_preview",
            message="Financial state changed after preview; preview again",
        )
    return CreditNotes.issue_with_evidence(
        db,
        CreditNoteIssueRequest(
            **payload.model_dump(),
            preview_fingerprint=preview_fingerprint,
            idempotency_key=idempotency_key,
        ),
    )
