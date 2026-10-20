"""Thin-client credit-note issuance through Starter ``dotmac-billing``.

Step 4 caller migration (ADR 0031): a credit note against a
Starter-issued invoice is issued by the pinned billing module as sole
ledger writer. Sub mirrors the credit document number onto its
``CreditNote`` row, records the structural ``StarterCreditNoteLink``,
and mirrors the invoice balance from the Starter collectible position.
No legacy funding ledger entry is written for Starter credits.

Eligibility is decided before any write and is deliberately narrow in
this slice: the credited invoice must be Starter-linked, the credit must
apply in full to that invoice at issue (no residual wallet credit), any
tax must be backed by one active tax rate whose snapshot reconciles
exactly, and the credit number must come from Starter numbering. An
eligible credit that Starter then refuses never falls back to the
legacy writer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from dotmac_billing import (
    AppliedTaxSnapshotV1,
    GetCurrentReceivablePositionV1,
    get_current_receivable_position,
)
from dotmac_billing.commands import IssueCreditNote
from dotmac_billing.errors import BillingError
from dotmac_billing.service import issue_credit_note
from dotmac_kernel.cache import TenantScope
from dotmac_kernel.money import Currency, Money, MoneyError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import Invoice, InvoiceStatus, StarterInvoiceLink, TaxRate
from app.services.billing.starter_issuance import _Numbering
from app.services.billing.starter_policies import (
    StarterBillingPolicies,
    resolve_starter_billing_policies,
    resolve_starter_credit_policies,
)
from app.services.domain_errors import DomainError
from app.services.operator_tenant import OPERATOR_TENANT_ID

__all__ = [
    "StarterCreditIssued",
    "StarterCreditNoteError",
    "credit_eligibility_reason",
    "issue_credit_note_via_starter",
    "mirror_invoice_after_starter_credit",
    "starter_invoice_link",
]

_SCOPE = TenantScope(OPERATOR_TENANT_ID)
_OWNER = "financial.starter_credit_notes"


class StarterCreditNoteError(DomainError, ValueError):
    """The Starter credit lane refused or cannot issue this credit."""


def _error(suffix: str, message: str, **details: object) -> StarterCreditNoteError:
    return StarterCreditNoteError(
        code=f"{_OWNER}.{suffix}", message=message, details=details
    )


@dataclass(frozen=True, slots=True)
class StarterCreditIssued:
    """Starter identities returned for one issued credit document."""

    document_id: UUID
    document_number: str
    billing_account_id: UUID
    collectible_after: Decimal


def starter_invoice_link(db: Session, invoice_id: UUID) -> StarterInvoiceLink | None:
    return db.execute(
        select(StarterInvoiceLink).where(StarterInvoiceLink.invoice_id == invoice_id)
    ).scalar_one_or_none()


def _clean(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _money(amount: Decimal, currency: Currency) -> Money:
    try:
        return Money.of(str(amount), currency)
    except (MoneyError, ArithmeticError, ValueError) as exc:
        raise _error(
            "amount_unrepresentable",
            "Credit amount cannot be represented at the currency scale",
            amount=str(amount),
            currency=currency.code,
        ) from exc


def _tax_snapshot(
    policies: StarterBillingPolicies,
    *,
    tax_rate: TaxRate,
    subtotal: Decimal,
    tax_total: Decimal,
    currency: Currency,
) -> AppliedTaxSnapshotV1:
    return AppliedTaxSnapshotV1(
        treatment_code=_clean(tax_rate.code),
        jurisdiction_code=policies.tax_jurisdiction,
        policy_id=policies.tax_policy_id,
        policy_version=policies.tax_policy_version,
        rate=tax_rate.rate,
        taxable_basis=_money(subtotal, currency),
        tax_amount=_money(tax_total, currency),
    )


def credit_eligibility_reason(
    db: Session,
    *,
    invoice: Invoice | None,
    credit_number: str | None,
    tax_total: Decimal,
    tax_rate_id: UUID | None,
    application_amount: Decimal,
    credit_total: Decimal,
    applies_to_invoice: bool,
) -> str | None:
    """Return why this credit cannot take the Starter lane yet, else None."""

    if invoice is None:
        return "unlinked credits stay on the legacy lane in this slice"
    if starter_invoice_link(db, invoice.id) is None:
        return "invoice is not Starter-issued"
    if _clean(credit_number):
        return "manually numbered credits stay on the legacy lane"
    if not applies_to_invoice:
        return "credit does not apply to its invoice at issue"
    if application_amount != credit_total:
        return "partially applied credits stay on the legacy lane in this slice"
    if tax_total > 0:
        if tax_rate_id is None:
            return "taxed credit needs its tax rate for Starter issuance"
        tax_rate = db.get(TaxRate, tax_rate_id)
        if tax_rate is None or not tax_rate.is_active or not _clean(tax_rate.code):
            return "credit tax rate is missing or inactive"
    return None


def issue_credit_note_via_starter(
    db: Session,
    *,
    invoice: Invoice,
    link: StarterInvoiceLink,
    subtotal: Decimal,
    tax_total: Decimal,
    total: Decimal,
    tax_rate_id: UUID | None,
    reason: str,
    actor_ref: str,
    idempotency_key: str,
    issued_at: datetime,
) -> StarterCreditIssued:
    """Issue one eligible credit through Starter as sole ledger writer.

    The caller owns the transaction boundary: on any failure nothing here
    may be committed, and the caller must not fall back to the legacy
    writer for this credit.
    """

    policies = resolve_starter_billing_policies(db)
    credit_policies = resolve_starter_credit_policies(db)
    currency = Currency(invoice.currency.upper(), policies.default_minor_units)
    snapshots: tuple[AppliedTaxSnapshotV1, ...] = ()
    if tax_total > 0:
        tax_rate = db.get(TaxRate, tax_rate_id) if tax_rate_id is not None else None
        assert tax_rate is not None  # eligibility proved presence
        snapshots = (
            _tax_snapshot(
                policies,
                tax_rate=tax_rate,
                subtotal=subtotal,
                tax_total=tax_total,
                currency=currency,
            ),
        )
    try:
        document = issue_credit_note(
            db,
            scope=_SCOPE,
            command=IssueCreditNote(
                original_document_id=link.document_id,
                pre_tax_amount=_money(subtotal, currency),
                tax_amount=_money(tax_total, currency),
                total_amount=_money(total, currency),
                reason=reason,
                series_code=policies.credit_note_series,
                reference_date=issued_at.date(),
                actor_ref=actor_ref,
                correlation_id=credit_policies.credit_correlation_id(idempotency_key),
                occurred_at=issued_at,
                document_profile_code=policies.credit_note_profile_code,
                document_profile_version=policies.document_profile_version,
                tax_snapshots=snapshots,
            ),
            numbering=_Numbering(policies),
        )
        current = get_current_receivable_position(
            db,
            scope=_SCOPE,
            request=GetCurrentReceivablePositionV1(
                scope=_SCOPE,
                billing_account_id=link.billing_account_id,
                currency=currency,
            ),
        )
    except StarterCreditNoteError:
        raise
    except BillingError as exc:
        raise _error(
            "starter_refused",
            "Starter billing refused this credit; it was not issued",
            invoice_id=str(invoice.id),
        ) from exc
    number = document.document_number
    if not number:
        raise _error(
            "starter_refused",
            "Starter billing issued no credit document number",
            invoice_id=str(invoice.id),
        )
    if current.position is None:
        raise _error(
            "position_unavailable",
            "Starter billing returned no receivable position after credit",
            invoice_id=str(invoice.id),
        )
    return StarterCreditIssued(
        document_id=document.id,
        document_number=number,
        billing_account_id=link.billing_account_id,
        collectible_after=current.position.collectible_receivable.amount,
    )


def mirror_invoice_after_starter_credit(
    invoice: Invoice,
    issued: StarterCreditIssued,
    *,
    settles_invoice: bool,
    issued_at: datetime,
) -> None:
    """Mirror balance/status from the Starter position; writes no ledger state."""
    invoice.balance_due = issued.collectible_after
    if settles_invoice:
        invoice.status = InvoiceStatus.paid
        invoice.paid_at = issued_at
