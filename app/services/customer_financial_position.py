"""Read-only customer financial position service.

This module is the shared query layer for customer-facing balances and billing
automation decisions. It does not post ledger entries, allocate payments, or
change invoice state.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.billing import Invoice
from app.services.common import coerce_uuid
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
    """Read projection whose financial meanings remain deliberately separate.

    Invoice receivables and prepaid service funding are not netted into a
    generic balance. Payment lifecycle and service-access state are owned by
    their respective services and are intentionally absent from this value.
    """

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


@dataclass(frozen=True)
class NativeCustomerFinancialBalance:
    """One currency-typed signed position from native financial events only.

    This is the shared accounting quantity beneath account-credit and prepaid
    funding decisions. It deliberately excludes the archived Splynx mirror.
    Callers must fail closed when ``other_currency_balances`` is non-empty;
    nominal amounts in different currencies are never netted together.
    """

    account_id: UUID
    currency: str
    available_balance: Decimal
    other_currency_balances: tuple[tuple[str, Decimal], ...]

    @property
    def automation_safe(self) -> bool:
        return not self.other_currency_balances


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


def get_native_customer_financial_balance(
    db: Session,
    account_id: object,
    *,
    currency: str | None = None,
) -> NativeCustomerFinancialBalance:
    """Return a native-only signed balance without legacy fallback.

    Credits are positive and collectible charges are negative. The Splynx
    archive is migration evidence, not a runtime input; reviewed opening
    positions are consumed separately by the prepaid reconstruction owner.
    """
    from app.services import display_format
    from app.services.common import round_money
    from app.services.customer_financial_ledger import (
        customer_financial_balances_by_currency,
    )

    account_uuid = coerce_uuid(account_id)
    unit = display_format.currency_code(currency or display_format.default_currency(db))
    raw = customer_financial_balances_by_currency(
        db,
        [account_uuid],
    ).get(account_uuid, {})
    normalized: dict[str, Decimal] = {}
    for code, amount in raw.items():
        normalized_code = display_format.currency_code(code)
        normalized[normalized_code] = round_money(
            normalized.get(normalized_code, Decimal("0.00")) + amount
        )
    other_currency_balances = tuple(
        sorted(
            (code, amount)
            for code, amount in normalized.items()
            if code != unit and amount != Decimal("0.00")
        )
    )
    return NativeCustomerFinancialBalance(
        account_id=account_uuid,
        currency=unit,
        available_balance=normalized.get(unit, Decimal("0.00")),
        other_currency_balances=other_currency_balances,
    )


def prepaid_available_balance(
    db: Session, account_id, *, currency: str | None = None
) -> Decimal:
    """Available service wallet balance consumed by access resolution.

    Funding authority is the reviewed opening balance plus native events after
    its timestamp. The optional currency resolves through the enforcement
    policy owner; nominal amounts from different currencies are never mixed.
    There is deliberately no Splynx or assumed-zero fallback.
    """
    from app.models.catalog import BillingMode
    from app.models.subscriber import Subscriber
    from app.services.billing_profile import resolve_billing_profile
    from app.services.prepaid_funding_reconstruction import (
        verified_prepaid_funding_balance,
    )

    account = db.get(Subscriber, coerce_uuid(account_id))
    if account is None:
        raise ValueError(f"Subscriber {account_id} was not found")
    profile = resolve_billing_profile(db, account)
    if profile.effective_mode != BillingMode.prepaid:
        return Decimal("0.00")
    if currency is None:
        from app.services.access_resolution import resolve_prepaid_enforcement_currency

        currency = resolve_prepaid_enforcement_currency(db)
    unit = str(currency).strip().upper()
    return verified_prepaid_funding_balance(db, account_id, currency=unit)


def prepaid_available_balances(
    db: Session,
    account_ids: Iterable[object],
    *,
    currency: str | None = None,
) -> dict[UUID, Decimal]:
    """Resolve reviewed opening balances plus native events for a cohort."""
    from app.services.prepaid_funding_reconstruction import (
        verified_prepaid_funding_balances,
    )

    if currency is None:
        from app.services.access_resolution import resolve_prepaid_enforcement_currency

        currency = resolve_prepaid_enforcement_currency(db)
    unit = str(currency).strip().upper()
    account_uuids = sorted(
        {coerce_uuid(account_id) for account_id in account_ids}, key=str
    )
    return verified_prepaid_funding_balances(db, account_uuids, currency=unit)


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
