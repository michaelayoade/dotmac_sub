"""Service helpers for billing ledger web routes."""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import Date, cast, func
from sqlalchemy.orm import joinedload

from app.models.billing import (
    Invoice,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
)
from app.models.splynx_transaction import SplynxBillingTransaction
from app.models.subscriber import Reseller, Subscriber
from app.services import web_billing_customers as web_billing_customers_service
from app.services.common import validate_enum

logger = logging.getLogger(__name__)


def _currency_code(value: object | None) -> str:
    code = str(value or "NGN").strip().upper()
    return code or "NGN"


def _format_currency_amount(amount: object, currency: object | None) -> str:
    return f"{_currency_code(currency)} {Decimal(str(amount or 0)):,.2f}"


def _format_currency_groups(amounts: dict[str, Decimal]) -> str:
    if not amounts:
        return _format_currency_amount(0, "NGN")
    return ", ".join(
        _format_currency_amount(amounts[currency], currency)
        for currency in sorted(amounts)
    )


def _add_grouped_amount(
    amounts: dict[str, Decimal], *, currency: object | None, amount: object
) -> None:
    code = _currency_code(currency)
    amounts[code] = amounts.get(code, Decimal("0")) + Decimal(str(amount or 0))


_CATEGORY_SOURCES: dict[str, tuple[LedgerSource, ...]] = {
    "service": (LedgerSource.invoice,),
    "payment": (LedgerSource.payment,),
    "credit_note": (LedgerSource.credit_note,),
    "adjustment": (LedgerSource.adjustment,),
    "refund": (LedgerSource.refund,),
    "other": (LedgerSource.other,),
}

# Legacy cutover: the migrated ledger carries invoice debits only through this
# instant. Native invoice issuance does NOT post a debit to ledger_entries (the
# invoice row itself is the AR record), so without merging post-cutover invoices
# the ledger view looks frozen at March 2026. Invoices issued on/before the
# cutover are already represented by migrated ledger rows — including them would
# double-count, so only issued_at strictly after this is merged.
_LEDGER_CUTOVER = datetime(2026, 3, 15, 23, 59, 59, tzinfo=UTC)


@dataclass(frozen=True)
class LedgerDateRange:
    start: datetime | None
    end: datetime | None
    start_date: date | None
    end_date: date | None


def _parse_date_range(
    start_date: str | None,
    end_date: str | None,
) -> LedgerDateRange:
    start_value = (start_date or "").strip()
    end_value = (end_date or "").strip()
    try:
        start_d = date.fromisoformat(start_value) if start_value else None
        end_d = date.fromisoformat(end_value) if end_value else None
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="start_date and end_date must be ISO dates"
        ) from exc

    if start_d and end_d and start_d > end_d:
        raise HTTPException(
            status_code=400,
            detail="start_date must be before or equal to end_date",
        )

    return LedgerDateRange(
        start=datetime.combine(start_d, time.min, tzinfo=UTC) if start_d else None,
        end=(
            datetime.combine(end_d + timedelta(days=1), time.min, tzinfo=UTC)
            if end_d
            else None
        ),
        start_date=start_d,
        end_date=end_d,
    )


def _invoice_as_ledger_row(invoice: Invoice) -> SimpleNamespace:
    """Adapt an Invoice into a display row matching the ledger template/CSV.

    Display-only: the amount shown is the invoice total (the charge); payments
    against it are already in ledger_entries as credits. Account balances/AR are
    NOT derived from this view — they come from invoices.balance_due.
    """
    label = invoice.memo or (
        f"Invoice {invoice.invoice_number}" if invoice.invoice_number else "Invoice"
    )
    return SimpleNamespace(
        id=invoice.id,
        account_id=invoice.account_id,
        account=invoice.account,
        entry_type=SimpleNamespace(value="debit"),
        source=SimpleNamespace(value="invoice"),
        amount=invoice.total,
        currency=invoice.currency or "NGN",
        memo=label,
        effective_date=invoice.issued_at,
        created_at=invoice.created_at,
        is_active=True,
    )


def _splynx_credit_as_ledger_row(txn, account) -> SimpleNamespace:  # type: ignore[no-untyped-def]
    """Adapt an unmigrated legacy credit transaction into a display row.

    Some pre-cutover credits (back-office corrections, credit notes,
    withholding-tax, service credits) were not imported 1:1 into ledger_entries;
    their VALUE is already reflected in each prepaid account's balance via the
    cutover deposit true-up, so they must NOT be inserted as real ledger rows
    (that would double-count the balance). This surfaces them in the view only —
    it never affects get_account_credit_balance or any total.
    """
    when = datetime(
        txn.transaction_date.year,
        txn.transaction_date.month,
        txn.transaction_date.day,
        tzinfo=UTC,
    )
    label = txn.description or txn.category_name or "Credit"
    return SimpleNamespace(
        id=txn.id,
        account_id=txn.subscriber_id,
        account=account,
        entry_type=SimpleNamespace(value="credit"),
        source=SimpleNamespace(value="credit_note"),
        amount=txn.amount,
        currency="NGN",
        memo=f"{label} (legacy import)",
        effective_date=when,
        created_at=when,
        is_active=True,
    )


def _display_date(entry) -> datetime:  # type: ignore[no-untyped-def]
    return getattr(entry, "effective_date", None) or entry.created_at


def build_ledger_entries_data(
    db,
    *,
    customer_ref: str | None,
    entry_type: str | None,
    start_date: str | None = None,
    end_date: str | None = None,
    category: str | None = None,
    partner_id: str | None = None,
    limit: int = 200,
) -> dict[str, object]:
    date_range = _parse_date_range(start_date, end_date)

    account_ids = []
    if customer_ref:
        account_ids = [
            UUID(item["id"])
            for item in web_billing_customers_service.accounts_for_customer(
                db, customer_ref
            )
        ]

    entries = []
    selected_partner_id = (partner_id or "").strip() or None
    # Only offer partners that actually own ledger activity. Listing every active
    # reseller surfaces empty/test partners (e.g. ones with zero subscribers),
    # and selecting one returns a blank ledger that reads as a broken filter.
    has_ledger_activity = (
        db.query(LedgerEntry.id)
        .join(Subscriber, Subscriber.id == LedgerEntry.account_id)
        .filter(Subscriber.reseller_id == Reseller.id)
        .filter(LedgerEntry.is_active.is_(True))
        .exists()
    )
    partner_options = [
        {"id": str(item.id), "name": item.name}
        for item in db.query(Reseller)
        .filter(Reseller.is_active.is_(True))
        .filter(has_ledger_activity)
        .order_by(Reseller.name.asc())
        .all()
    ]
    want_type = (
        validate_enum(entry_type, LedgerEntryType, "entry_type") if entry_type else None
    )
    selected_category = (category or "").strip().lower()

    if account_ids or not customer_ref:
        query = (
            db.query(LedgerEntry)
            .options(joinedload(LedgerEntry.account))
            .filter(LedgerEntry.is_active.is_(True))
        )
        if account_ids:
            query = query.filter(LedgerEntry.account_id.in_(account_ids))
        if want_type is not None:
            query = query.filter(LedgerEntry.entry_type == want_type)
        if selected_partner_id:
            query = query.filter(
                LedgerEntry.account.has(
                    Subscriber.reseller_id == UUID(selected_partner_id)
                )
            )
        if selected_category in _CATEGORY_SOURCES:
            query = query.filter(
                LedgerEntry.source.in_(_CATEGORY_SOURCES[selected_category])
            )
        ledger_date = func.coalesce(LedgerEntry.effective_date, LedgerEntry.created_at)
        if date_range.start is not None:
            query = query.filter(ledger_date >= date_range.start)
        if date_range.end is not None:
            query = query.filter(ledger_date < date_range.end)
        ledger_rows = query.order_by(ledger_date.desc()).limit(limit).all()

        # Merge post-cutover invoices as synthetic debit rows so the ledger view
        # reflects ongoing billing (native invoices don't post to ledger_entries).
        # Invoices are debits categorised as "service", so only include them when
        # the active filters don't exclude that combination.
        invoice_rows: list[SimpleNamespace] = []
        if (want_type in (None, LedgerEntryType.debit)) and (
            selected_category in ("", "service")
        ):
            inv_q = (
                db.query(Invoice)
                .options(joinedload(Invoice.account))
                .filter(Invoice.is_active.is_(True))
                .filter(Invoice.is_proforma.is_(False))
                .filter(
                    Invoice.status.notin_([InvoiceStatus.void, InvoiceStatus.draft])
                )
                .filter(Invoice.issued_at.isnot(None))
                .filter(Invoice.issued_at > _LEDGER_CUTOVER)
            )
            if account_ids:
                inv_q = inv_q.filter(Invoice.account_id.in_(account_ids))
            if selected_partner_id:
                inv_q = inv_q.filter(
                    Invoice.account.has(
                        Subscriber.reseller_id == UUID(selected_partner_id)
                    )
                )
            if date_range.start is not None:
                inv_q = inv_q.filter(Invoice.issued_at >= date_range.start)
            if date_range.end is not None:
                inv_q = inv_q.filter(Invoice.issued_at < date_range.end)
            invoice_rows = [
                _invoice_as_ledger_row(invoice)
                for invoice in inv_q.order_by(Invoice.issued_at.desc())
                .limit(limit)
                .all()
            ]

        # Merge pre-cutover credits that were never migrated 1:1 into the
        # ledger (corrections / credit notes / withholding-tax / service credits),
        # so they remain visible for audit. Display-only: deduped
        # against existing ledger credits (same account+amount+date) so already-
        # migrated ones aren't shown twice; never written to ledger_entries, so
        # balances are untouched. They are credits, shown under "credit_note".
        # These legacy credits are all pre-cutover (<= 2026-03-15), so in an
        # unscoped, recency-ordered view they can never beat the post-cutover
        # top-N — running the (correlated) dedup scan there is pure cost for zero
        # rows. Only evaluate it when the view is scoped to a customer/partner or
        # a date range, i.e. when a pre-cutover row could actually surface.
        is_scoped = bool(
            account_ids or selected_partner_id or date_range.start or date_range.end
        )
        splynx_credit_rows: list[SimpleNamespace] = []
        if (
            is_scoped
            and (want_type in (None, LedgerEntryType.credit))
            and (selected_category in ("", "credit_note"))
        ):
            already_in_ledger = (
                db.query(LedgerEntry.id)
                .filter(
                    LedgerEntry.account_id == SplynxBillingTransaction.subscriber_id
                )
                .filter(LedgerEntry.is_active.is_(True))
                .filter(LedgerEntry.entry_type == LedgerEntryType.credit)
                .filter(LedgerEntry.amount == SplynxBillingTransaction.amount)
                .filter(
                    cast(
                        func.coalesce(
                            LedgerEntry.effective_date, LedgerEntry.created_at
                        ),
                        Date,
                    )
                    == SplynxBillingTransaction.transaction_date
                )
                .exists()
            )
            sx_q = (
                db.query(SplynxBillingTransaction, Subscriber)
                .join(
                    Subscriber,
                    Subscriber.id == SplynxBillingTransaction.subscriber_id,
                )
                .filter(SplynxBillingTransaction.deleted.is_(False))
                .filter(SplynxBillingTransaction.entry_type == "credit")
                .filter(SplynxBillingTransaction.splynx_payment_id.is_(None))
                .filter(SplynxBillingTransaction.transaction_date.isnot(None))
                .filter(
                    SplynxBillingTransaction.transaction_date <= _LEDGER_CUTOVER.date()
                )
                .filter(~already_in_ledger)
            )
            if account_ids:
                sx_q = sx_q.filter(
                    SplynxBillingTransaction.subscriber_id.in_(account_ids)
                )
            if selected_partner_id:
                sx_q = sx_q.filter(Subscriber.reseller_id == UUID(selected_partner_id))
            if date_range.start_date is not None:
                sx_q = sx_q.filter(
                    SplynxBillingTransaction.transaction_date >= date_range.start_date
                )
            if date_range.end_date is not None:
                sx_q = sx_q.filter(
                    SplynxBillingTransaction.transaction_date <= date_range.end_date
                )
            splynx_credit_rows = [
                _splynx_credit_as_ledger_row(txn, account)
                for txn, account in sx_q.order_by(
                    SplynxBillingTransaction.transaction_date.desc()
                )
                .limit(limit)
                .all()
            ]

        entries = sorted(
            [*ledger_rows, *invoice_rows, *splynx_credit_rows],
            key=_display_date,
            reverse=True,
        )[:limit]

    credit_entries = [
        entry
        for entry in entries
        if getattr(getattr(entry, "entry_type", None), "value", None) == "credit"
    ]
    debit_entries = [
        entry
        for entry in entries
        if getattr(getattr(entry, "entry_type", None), "value", None) == "debit"
    ]
    credit_amounts: dict[str, Decimal] = {}
    debit_amounts: dict[str, Decimal] = {}
    for entry in credit_entries:
        _add_grouped_amount(
            credit_amounts,
            currency=getattr(entry, "currency", None),
            amount=getattr(entry, "amount", 0),
        )
    for entry in debit_entries:
        _add_grouped_amount(
            debit_amounts,
            currency=getattr(entry, "currency", None),
            amount=getattr(entry, "amount", 0),
        )
    net_amounts = dict(credit_amounts)
    for currency, amount in debit_amounts.items():
        net_amounts[currency] = net_amounts.get(currency, Decimal("0")) - amount
    credit_total = sum(float(amount) for amount in credit_amounts.values())
    debit_total = sum(float(amount) for amount in debit_amounts.values())
    ledger_totals = {
        "credit_count": len(credit_entries),
        "credit_total": credit_total,
        "credit_amounts": credit_amounts,
        "credit_display": _format_currency_groups(credit_amounts),
        "debit_count": len(debit_entries),
        "debit_total": debit_total,
        "debit_amounts": debit_amounts,
        "debit_display": _format_currency_groups(debit_amounts),
        "net_total": credit_total - debit_total,
        "net_amounts": net_amounts,
        "net_display": _format_currency_groups(net_amounts),
    }

    return {
        "entries": entries,
        "ledger_totals": ledger_totals,
        "entry_type": entry_type,
        "customer_ref": customer_ref,
        "start_date": date_range.start_date.isoformat()
        if date_range.start_date
        else "",
        "end_date": date_range.end_date.isoformat() if date_range.end_date else "",
        "category": category,
        "selected_partner_id": selected_partner_id,
        "partner_options": partner_options,
    }


def _entry_customer_name(entry: LedgerEntry) -> str:
    account = getattr(entry, "account", None)
    if account is None:
        return ""
    return str(getattr(account, "name", "") or "").strip()


def render_ledger_csv(entries: list[LedgerEntry]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "entry_id",
            "customer_name",
            "entry_type",
            "source",
            "debit_amount",
            "credit_amount",
            "currency",
            "description",
            "date",
        ]
    )
    for entry in entries:
        entry_type = getattr(getattr(entry, "entry_type", None), "value", "") or ""
        amount = Decimal(str(getattr(entry, "amount", 0) or 0))
        # Prefer the real transaction date; created_at is the import instant for
        # migrated rows and would mislabel every one as 2026-03-15.
        entry_date = getattr(entry, "effective_date", None) or entry.created_at
        writer.writerow(
            [
                str(entry.id),
                _entry_customer_name(entry),
                entry_type,
                getattr(getattr(entry, "source", None), "value", "") or "",
                f"{amount:.2f}" if entry_type == "debit" else "",
                f"{amount:.2f}" if entry_type == "credit" else "",
                entry.currency or "NGN",
                entry.memo or "",
                entry_date.isoformat() if entry_date else "",
            ]
        )
    return buffer.getvalue()
