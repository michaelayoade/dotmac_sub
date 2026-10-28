"""Thin-client reads of Starter document facts and receivable positions.

Readers lane (ADR 0031): for Starter-linked invoices the frozen Starter
document fact is the document truth and the Starter receivable position
is the balance truth. This module reads both through the pinned
``dotmac-billing`` contracts and never writes. Legacy invoices, and
Starter-linked invoices whose issuance exposed no exact fact version
(multi-line receipts), keep rendering from the Sub mirror rows written
at issuance. A Starter refusal for a linked, fact-versioned invoice
propagates: readers never silently fall back to legacy numbers for a
document Starter issued.

Reads are not gated by the D1 ``starter_ledger_live_enabled`` switch:
that switch gates new issuance only; already-linked documents always
read from Starter.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

from dotmac_billing import (
    GetCurrentReceivablePositionV1,
    GetInvoiceDocumentFactV1,
    InvoiceDocumentFactV1,
    get_current_receivable_position,
    get_invoice_document_fact,
)
from dotmac_billing.errors import BillingError
from dotmac_kernel.cache import TenantScope
from dotmac_kernel.money import Currency
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import Invoice, StarterInvoiceLink
from app.services.billing.starter_policies import resolve_starter_billing_policies
from app.services.domain_errors import DomainError
from app.services.operator_tenant import OPERATOR_TENANT_ID

__all__ = [
    "StarterReaderError",
    "apply_fact_overlay",
    "starter_account_collectible",
    "starter_invoice_fact",
    "starter_invoice_link",
]

_SCOPE = TenantScope(OPERATOR_TENANT_ID)
_OWNER = "financial.starter_positions"


class StarterReaderError(DomainError, ValueError):
    """A Starter document fact or position could not be read."""


def _error(suffix: str, message: str, **details: object) -> StarterReaderError:
    return StarterReaderError(
        code=f"{_OWNER}.{suffix}", message=message, details=details
    )


def starter_invoice_link(db: Session, invoice_id: UUID) -> StarterInvoiceLink | None:
    return db.execute(
        select(StarterInvoiceLink).where(StarterInvoiceLink.invoice_id == invoice_id)
    ).scalar_one_or_none()


def starter_invoice_fact(db: Session, invoice: Invoice) -> InvoiceDocumentFactV1 | None:
    """Exact frozen document fact for a Starter-linked invoice, if readable.

    Returns ``None`` when the invoice is not Starter-linked or its
    issuance exposed no exact fact version. A Starter refusal raises.
    """
    link = starter_invoice_link(db, invoice.id)
    if link is None or link.issued_fact_version is None:
        return None
    try:
        stored = get_invoice_document_fact(
            db,
            scope=_SCOPE,
            request=GetInvoiceDocumentFactV1(
                scope=_SCOPE,
                invoice_id=link.document_id,
                fact_version=link.issued_fact_version,
            ),
        )
    except BillingError as exc:
        raise _error(
            "fact_unavailable",
            "Starter billing refused the document fact read",
            invoice_id=str(invoice.id),
        ) from exc
    return stored.fact


def starter_account_collectible(
    db: Session, *, billing_account_id: UUID, currency_code: str
) -> Decimal:
    """Current collectible amount for one Starter billing account."""
    policies = resolve_starter_billing_policies(db)
    currency = Currency(currency_code.upper(), policies.default_minor_units)
    try:
        current = get_current_receivable_position(
            db,
            scope=_SCOPE,
            request=GetCurrentReceivablePositionV1(
                scope=_SCOPE,
                billing_account_id=billing_account_id,
                currency=currency,
            ),
        )
    except BillingError as exc:
        raise _error(
            "position_unavailable",
            "Starter billing refused the receivable position read",
            billing_account_id=str(billing_account_id),
        ) from exc
    if current.position is None:
        return Decimal("0")
    return current.position.collectible_receivable.amount


class _FactInvoiceView:
    """Read-only invoice view: fact fields win, everything else delegates.

    Renderers only read attributes, so no ORM object is copied or
    constructed: number, totals, dates, and lines come from the frozen
    fact while identity, account, status, and balance delegate to the
    persistent mirror row (balance/status stay mirror-owned; the fact
    carries neither).
    """

    def __init__(self, base: Invoice, fact: InvoiceDocumentFactV1) -> None:
        self._base = base
        self.invoice_number = fact.document_number
        self.subtotal = fact.subtotal.amount
        self.tax_total = fact.tax_total.amount
        self.total = fact.grand_total.amount
        self.issued_at = fact.issued_at
        self.due_at = fact.due_at
        self.tax_rate_percent_snapshots = tuple(
            tax.rate * Decimal("100") for tax in fact.tax_snapshots
        )
        self.lines = [
            SimpleNamespace(
                description=line.description,
                quantity=line.quantity,
                unit_price=line.unit_amount.amount,
                amount=line.pre_tax_amount.amount,
                is_active=True,
            )
            for line in fact.lines
        ]

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)


def apply_fact_overlay(invoice: Invoice, fact: InvoiceDocumentFactV1) -> Invoice:
    """Transient render view carrying the frozen fact's document fields."""
    return cast(Invoice, _FactInvoiceView(invoice, fact))
