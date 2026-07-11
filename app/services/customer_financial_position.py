"""Read-only customer financial position service.

This module is the shared query layer for customer-facing balances and billing
automation decisions. It does not post ledger entries, allocate payments, or
change invoice state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.billing import Invoice
from app.services.invoice_collectibility import (
    collection_blocking_balance,
    due_invoice_balance,
    list_open_invoices,
    open_invoice_balance,
    overdue_debt_balance,
    overdue_status_count,
)


@dataclass(frozen=True)
class CustomerFinancialPosition:
    account_id: object
    open_invoice_balance: Decimal
    due_invoice_balance: Decimal
    overdue_debt_balance: Decimal
    collection_blocking_balance: Decimal
    overdue_invoice_count: int
    prepaid_available_balance: Decimal
    oldest_due_invoice: Invoice | None
    days_overdue: int
    currency: str

    @property
    def has_open_debt(self) -> bool:
        return self.open_invoice_balance > Decimal("0.00")

    @property
    def has_due_debt(self) -> bool:
        return self.due_invoice_balance > Decimal("0.00")

    @property
    def has_overdue_debt(self) -> bool:
        return self.overdue_debt_balance > Decimal("0.00")

    @property
    def has_collection_blocking_debt(self) -> bool:
        return self.collection_blocking_balance > Decimal("0.00")

    @property
    def net_prepaid_position(self) -> Decimal:
        return self.prepaid_available_balance - self.open_invoice_balance


def get_customer_financial_position(
    db: Session,
    account_id,
    *,
    now: datetime | None = None,
    include_prepaid_balance: bool = True,
) -> CustomerFinancialPosition:
    now = _as_aware(now)
    invoices = list_open_invoices(db, account_id)
    oldest_due = _oldest_due_invoice(invoices, now)
    return CustomerFinancialPosition(
        account_id=account_id,
        open_invoice_balance=open_invoice_balance(db, account_id),
        due_invoice_balance=due_invoice_balance(db, account_id, now=now),
        overdue_debt_balance=overdue_debt_balance(db, account_id, now=now),
        collection_blocking_balance=collection_blocking_balance(db, account_id),
        overdue_invoice_count=overdue_status_count(db, account_id),
        prepaid_available_balance=(
            prepaid_available_balance(db, account_id)
            if include_prepaid_balance
            else Decimal("0.00")
        ),
        oldest_due_invoice=oldest_due,
        days_overdue=_days_overdue(oldest_due, now),
        currency=_currency(invoices),
    )


def prepaid_available_balance(db: Session, account_id) -> Decimal:
    """Available service wallet balance, matching collections enforcement."""
    from app.services.collections import get_available_balance

    return get_available_balance(db, str(account_id))


def _oldest_due_invoice(invoices: list[Invoice], now: datetime) -> Invoice | None:
    due = [
        invoice
        for invoice in invoices
        if invoice.due_at is not None and _as_aware(invoice.due_at).date() < now.date()
    ]
    return due[0] if due else None


def _days_overdue(invoice: Invoice | None, now: datetime) -> int:
    if invoice is None or invoice.due_at is None:
        return 0
    return max(0, (now.date() - _as_aware(invoice.due_at).date()).days)


def _currency(invoices: list[Invoice]) -> str:
    for invoice in invoices:
        if invoice.currency:
            return invoice.currency
    return "NGN"


def _as_aware(value: datetime | None) -> datetime:
    value = value or datetime.now(UTC)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value
