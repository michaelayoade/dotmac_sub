"""Read-only reconciliation of legacy subscription VAT billing evidence.

This compatibility resolver identifies issued invoices that may need operator
review after the recurring-billing exemption fix and the correction of the
legacy ``with_vat`` form label.  It never mutates an invoice and never decides
that a credit note exists: confirmed corrections are delegated to
``financial.credit_notes`` through the web application coordinator.

The current ``CustomerTaxPolicy`` row is not historical evidence.  Therefore
an exemption is confirmed only when the invoice tax point is on or after that
row's current ``updated_at`` value and every active tax-bearing line belongs to
a subscription.  Older or mixed invoices remain explicit review candidates.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models.billing import (
    CreditNote,
    CreditNoteStatus,
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    TaxApplication,
)
from app.models.catalog import CatalogOffer, Subscription
from app.models.customer_tax_policy import CustomerTaxPolicy
from app.services.common import round_money

_ISSUED_EVIDENCE_STATUSES = frozenset(
    {
        InvoiceStatus.issued,
        InvoiceStatus.partially_paid,
        InvoiceStatus.paid,
        InvoiceStatus.overdue,
        InvoiceStatus.written_off,
    }
)
_ADJUSTING_CREDIT_STATUSES = frozenset(
    {
        CreditNoteStatus.issued,
        CreditNoteStatus.partially_applied,
        CreditNoteStatus.applied,
    }
)
_ZERO = Decimal("0.00")


class TaxReconciliationReason(StrEnum):
    confirmed_customer_exemption = "confirmed_customer_exemption"
    exemption_timing_unproven = "exemption_timing_unproven"
    mixed_invoice_tax_scope = "mixed_invoice_tax_scope"
    inclusive_label_ambiguity = "inclusive_label_ambiguity"


class TaxReconciliationConfidence(StrEnum):
    confirmed = "confirmed"
    review_required = "review_required"


@dataclass(frozen=True)
class TaxReconciliationCandidate:
    invoice_id: UUID
    invoice_number: str | None
    account_id: UUID
    account_name: str
    currency: str
    tax_point: datetime
    observed_tax_total: Decimal
    credited_tax_total: Decimal
    maximum_remaining_adjustment: Decimal
    reason: TaxReconciliationReason
    confidence: TaxReconciliationConfidence
    customer_tax_policy_id: UUID | None
    customer_tax_policy_version: int | None
    customer_tax_policy_updated_at: datetime | None
    source_tax_rate_id: UUID | None
    remediation_owner: str
    fingerprint: str

    @property
    def can_prepare_tax_credit(self) -> bool:
        return self.confidence == TaxReconciliationConfidence.confirmed


@dataclass(frozen=True)
class TaxReconciliationPage:
    candidates: tuple[TaxReconciliationCandidate, ...]
    offset: int
    limit: int
    has_more: bool


def _fingerprint(
    *,
    invoice: Invoice,
    policy: CustomerTaxPolicy | None,
    reason: TaxReconciliationReason,
    observed_tax_total: Decimal,
    credited_tax_total: Decimal,
    remaining: Decimal,
) -> str:
    payload = {
        "invoice_id": str(invoice.id),
        "invoice_updated_at": invoice.updated_at.isoformat(),
        "invoice_status": invoice.status.value,
        "invoice_tax_total": str(observed_tax_total),
        "credited_tax_total": str(credited_tax_total),
        "remaining": str(remaining),
        "reason": reason.value,
        "policy_id": str(policy.id) if policy else None,
        "policy_version": policy.version if policy else None,
        "policy_updated_at": policy.updated_at.isoformat() if policy else None,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _candidate_query(*, invoice_id: UUID | None = None):
    tax_point = func.coalesce(Invoice.issued_at, Invoice.created_at)
    taxed_subscription_line = exists(
        select(InvoiceLine.id)
        .where(InvoiceLine.invoice_id == Invoice.id)
        .where(InvoiceLine.is_active.is_(True))
        .where(InvoiceLine.subscription_id.is_not(None))
        .where(InvoiceLine.tax_rate_id.is_not(None))
    )
    taxed_non_subscription_line = exists(
        select(InvoiceLine.id)
        .where(InvoiceLine.invoice_id == Invoice.id)
        .where(InvoiceLine.is_active.is_(True))
        .where(InvoiceLine.subscription_id.is_(None))
        .where(InvoiceLine.tax_rate_id.is_not(None))
    )
    legacy_inclusive_label_line = exists(
        select(InvoiceLine.id)
        .join(Subscription, Subscription.id == InvoiceLine.subscription_id)
        .join(CatalogOffer, CatalogOffer.id == Subscription.offer_id)
        .where(InvoiceLine.invoice_id == Invoice.id)
        .where(InvoiceLine.is_active.is_(True))
        .where(InvoiceLine.tax_rate_id.is_not(None))
        .where(InvoiceLine.tax_application == TaxApplication.exclusive)
        .where(CatalogOffer.with_vat.is_(True))
    )
    credited_tax_total = (
        select(func.coalesce(func.sum(CreditNote.tax_total), _ZERO))
        .where(CreditNote.invoice_id == Invoice.id)
        .where(CreditNote.is_active.is_(True))
        .where(CreditNote.status.in_(_ADJUSTING_CREDIT_STATUSES))
        .correlate(Invoice)
        .scalar_subquery()
    )
    source_tax_rate_id = (
        select(InvoiceLine.tax_rate_id)
        .where(InvoiceLine.invoice_id == Invoice.id)
        .where(InvoiceLine.is_active.is_(True))
        .where(InvoiceLine.tax_rate_id.is_not(None))
        .order_by(InvoiceLine.tax_rate_id)
        .limit(1)
        .correlate(Invoice)
        .scalar_subquery()
    )
    source_tax_rate_count = (
        select(func.count(func.distinct(InvoiceLine.tax_rate_id)))
        .where(InvoiceLine.invoice_id == Invoice.id)
        .where(InvoiceLine.is_active.is_(True))
        .where(InvoiceLine.tax_rate_id.is_not(None))
        .correlate(Invoice)
        .scalar_subquery()
    )
    query = (
        select(
            Invoice,
            CustomerTaxPolicy,
            credited_tax_total.label("credited_tax_total"),
            taxed_non_subscription_line.label("has_taxed_non_subscription_line"),
            legacy_inclusive_label_line.label("has_legacy_inclusive_label_line"),
            source_tax_rate_id.label("source_tax_rate_id"),
            source_tax_rate_count.label("source_tax_rate_count"),
        )
        .outerjoin(
            CustomerTaxPolicy,
            CustomerTaxPolicy.account_id == Invoice.account_id,
        )
        .options(selectinload(Invoice.account))
        .where(Invoice.is_active.is_(True))
        .where(Invoice.is_proforma.is_(False))
        .where(Invoice.status.in_(_ISSUED_EVIDENCE_STATUSES))
        .where(Invoice.tax_total > 0)
        .where(credited_tax_total < Invoice.tax_total)
        .where(
            or_(
                and_(
                    CustomerTaxPolicy.vat_exempt.is_(True),
                    taxed_subscription_line,
                ),
                legacy_inclusive_label_line,
            )
        )
        .order_by(tax_point.desc(), Invoice.id.desc())
    )
    if invoice_id is not None:
        query = query.where(Invoice.id == invoice_id)
    return query


def _to_candidate(
    *,
    invoice: Invoice,
    policy: CustomerTaxPolicy | None,
    credited_tax_total: Decimal,
    has_taxed_non_subscription_line: bool,
    has_legacy_inclusive_label_line: bool,
    source_tax_rate_id: UUID | None,
    source_tax_rate_count: int,
) -> TaxReconciliationCandidate:
    tax_point = invoice.issued_at or invoice.created_at
    observed = round_money(invoice.tax_total)
    credited = round_money(credited_tax_total)
    remaining = round_money(max(_ZERO, observed - credited))

    if policy is not None and policy.vat_exempt:
        if tax_point < policy.updated_at:
            reason = TaxReconciliationReason.exemption_timing_unproven
        elif has_taxed_non_subscription_line:
            reason = TaxReconciliationReason.mixed_invoice_tax_scope
        else:
            reason = TaxReconciliationReason.confirmed_customer_exemption
    elif has_legacy_inclusive_label_line:
        reason = TaxReconciliationReason.inclusive_label_ambiguity
    else:  # pragma: no cover - the SQL predicate makes this unreachable
        raise RuntimeError("Tax reconciliation candidate lost its qualifying fact")

    confidence = (
        TaxReconciliationConfidence.confirmed
        if reason == TaxReconciliationReason.confirmed_customer_exemption
        else TaxReconciliationConfidence.review_required
    )
    account_name = invoice.account.name if invoice.account else str(invoice.account_id)
    return TaxReconciliationCandidate(
        invoice_id=invoice.id,
        invoice_number=invoice.invoice_number,
        account_id=invoice.account_id,
        account_name=account_name,
        currency=invoice.currency,
        tax_point=tax_point,
        observed_tax_total=observed,
        credited_tax_total=credited,
        maximum_remaining_adjustment=remaining,
        reason=reason,
        confidence=confidence,
        customer_tax_policy_id=policy.id if policy else None,
        customer_tax_policy_version=policy.version if policy else None,
        customer_tax_policy_updated_at=policy.updated_at if policy else None,
        source_tax_rate_id=source_tax_rate_id if source_tax_rate_count == 1 else None,
        remediation_owner="financial.credit_notes",
        fingerprint=_fingerprint(
            invoice=invoice,
            policy=policy,
            reason=reason,
            observed_tax_total=observed,
            credited_tax_total=credited,
            remaining=remaining,
        ),
    )


def list_tax_reconciliation_candidates(
    db: Session,
    *,
    offset: int = 0,
    limit: int = 100,
) -> TaxReconciliationPage:
    """Return a bounded, newest-first page of unresolved candidates."""

    bounded_offset = max(0, offset)
    bounded_limit = min(max(1, limit), 200)
    rows = db.execute(
        _candidate_query().offset(bounded_offset).limit(bounded_limit + 1)
    ).all()
    candidates = tuple(
        _to_candidate(
            invoice=row[0],
            policy=row[1],
            credited_tax_total=row[2],
            has_taxed_non_subscription_line=bool(row[3]),
            has_legacy_inclusive_label_line=bool(row[4]),
            source_tax_rate_id=row[5],
            source_tax_rate_count=int(row[6]),
        )
        for row in rows[:bounded_limit]
    )
    return TaxReconciliationPage(
        candidates=candidates,
        offset=bounded_offset,
        limit=bounded_limit,
        has_more=len(rows) > bounded_limit,
    )


def get_tax_reconciliation_candidate(
    db: Session, invoice_id: UUID
) -> TaxReconciliationCandidate | None:
    """Recompute one candidate so confirmations fail closed on drift."""

    row = db.execute(_candidate_query(invoice_id=invoice_id)).one_or_none()
    if row is None:
        return None
    return _to_candidate(
        invoice=row[0],
        policy=row[1],
        credited_tax_total=row[2],
        has_taxed_non_subscription_line=bool(row[3]),
        has_legacy_inclusive_label_line=bool(row[4]),
        source_tax_rate_id=row[5],
        source_tax_rate_count=int(row[6]),
    )
