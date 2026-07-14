"""Canonical customer financial ledger.

This module is the customer-facing money source of truth. It emits real customer
financial events only: legacy mirror transactions, succeeded payments, service
charges, credit notes, refunds, and approved manual adjustments. Internal
cutover/remediation rows stay in the operational audit trail but are excluded
from this ledger by design.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from sqlalchemy import and_, case, exists, func, literal, or_
from sqlalchemy.orm import Session, load_only

from app.models.billing import (
    CreditNote,
    CreditNoteStatus,
    Invoice,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentAllocation,
    PaymentStatus,
)
from app.models.splynx_transaction import SplynxBillingTransaction
from app.services.billing.invoice_classification import collectible_ar_invoice_filter
from app.services.common import coerce_uuid, round_money

LEGACY_LEDGER_CUTOVER = datetime(2026, 3, 15, 23, 59, 59, tzinfo=UTC)
PAYMENT_ACTIVITY_AT = datetime(2026, 6, 16, tzinfo=UTC)
SERVICE_ACTIVITY_AT = datetime(2026, 6, 16, 9, 8, tzinfo=UTC)

INTERNAL_MEMO_EXACT = {
    "Prepaid opening balance @ cutover",
}
INTERNAL_MEMO_PREFIXES = (
    "Correction:",
    "Partial cutover opening balance construction adjustment",
    "Reversal of phantom",
    "Reversal of prepaid opening",
    "Data repair 2026-06-29:",
    "Validated account credit consumed",
)


@dataclass(frozen=True)
class CustomerFinancialEvent:
    id: str
    account_id: UUID
    entry_type: LedgerEntryType
    source: LedgerSource
    amount: Decimal
    currency: str
    memo: str
    occurred_at: datetime
    raw: Any | None = None

    @property
    def signed_amount(self) -> Decimal:
        amount = Decimal(str(self.amount or 0))
        return amount if self.entry_type == LedgerEntryType.credit else -amount

    @property
    def created_at(self) -> datetime:
        return self.occurred_at

    @property
    def effective_date(self) -> datetime:
        return self.occurred_at

    def statement_entry(self) -> SimpleNamespace:
        return SimpleNamespace(
            id=self.id,
            account_id=self.account_id,
            entry_type=SimpleNamespace(value=self.entry_type.value),
            source=SimpleNamespace(value=self.source.value),
            amount=self.amount,
            currency=self.currency,
            memo=self.memo,
            effective_date=self.occurred_at,
            created_at=self.occurred_at,
            is_active=True,
        )


def _money(value: object) -> Decimal:
    return round_money(Decimal(str(value or 0)))


def _event_date(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _memo_is_internal(memo: str | None) -> bool:
    text = str(memo or "")
    return text in INTERNAL_MEMO_EXACT or text.startswith(INTERNAL_MEMO_PREFIXES)


def _has_legacy_mirror(db: Session, account_id: UUID) -> bool:
    return bool(
        db.query(
            exists().where(
                and_(
                    SplynxBillingTransaction.subscriber_id == account_id,
                    SplynxBillingTransaction.deleted.is_(False),
                )
            )
        ).scalar()
    )


def _in_window(
    occurred_at: datetime, *, start: datetime | None, end: datetime | None
) -> bool:
    if start is not None and occurred_at < start:
        return False
    return not (end is not None and occurred_at >= end)


def _legacy_event(txn: SplynxBillingTransaction) -> CustomerFinancialEvent | None:
    if txn.transaction_date is None or txn.subscriber_id is None:
        return None
    occurred_at = datetime(
        txn.transaction_date.year,
        txn.transaction_date.month,
        txn.transaction_date.day,
        tzinfo=UTC,
    )
    entry_type = (
        LedgerEntryType.credit
        if str(txn.entry_type or "") == LedgerEntryType.credit.value
        else LedgerEntryType.debit
    )
    if txn.splynx_payment_id is not None:
        source = LedgerSource.payment
    elif txn.splynx_credit_note_id is not None:
        source = LedgerSource.credit_note
    elif txn.splynx_invoice_id is not None or entry_type == LedgerEntryType.debit:
        source = LedgerSource.invoice
    else:
        source = LedgerSource.other
    return CustomerFinancialEvent(
        id=f"splynx:{txn.id}",
        account_id=txn.subscriber_id,
        entry_type=entry_type,
        source=source,
        amount=_money(txn.amount),
        currency="NGN",
        memo=txn.description or txn.category_name or "Legacy transaction",
        occurred_at=occurred_at,
        raw=txn,
    )


def _payment_event(payment: Payment) -> CustomerFinancialEvent | None:
    net_amount = _money(payment.amount) - _money(payment.refunded_amount)
    if net_amount <= 0 or payment.account_id is None:
        return None
    return CustomerFinancialEvent(
        id=f"payment:{payment.id}",
        account_id=payment.account_id,
        entry_type=LedgerEntryType.credit,
        source=LedgerSource.payment,
        amount=net_amount,
        currency=payment.currency or "NGN",
        memo=payment.memo or payment.receipt_number or "Payment received",
        occurred_at=_event_date(payment.paid_at or payment.created_at),
        raw=payment,
    )


def _external_allocation_event(
    allocation: PaymentAllocation,
) -> CustomerFinancialEvent | None:
    invoice = allocation.invoice
    payment = allocation.payment
    if invoice is None or payment is None:
        return None
    if payment.account_id == invoice.account_id:
        return None
    return CustomerFinancialEvent(
        id=f"external-allocation:{allocation.id}",
        account_id=invoice.account_id,
        entry_type=LedgerEntryType.credit,
        source=LedgerSource.payment,
        amount=_money(allocation.amount),
        currency=payment.currency or invoice.currency or "NGN",
        memo=allocation.memo or payment.memo or "Payment allocated to invoice",
        occurred_at=_event_date(allocation.created_at),
        raw=allocation,
    )


def _invoice_event(invoice: Invoice) -> CustomerFinancialEvent:
    return CustomerFinancialEvent(
        id=f"invoice:{invoice.id}",
        account_id=invoice.account_id,
        entry_type=LedgerEntryType.debit,
        source=LedgerSource.invoice,
        amount=_money(invoice.total),
        currency=invoice.currency or "NGN",
        memo=invoice.memo
        or (
            f"Invoice {invoice.invoice_number}"
            if invoice.invoice_number
            else "Service invoice"
        ),
        occurred_at=_event_date(invoice.issued_at or invoice.created_at),
        raw=invoice,
    )


def _credit_note_event(note: CreditNote) -> CustomerFinancialEvent:
    return CustomerFinancialEvent(
        id=f"credit-note:{note.id}",
        account_id=note.account_id,
        entry_type=LedgerEntryType.credit,
        source=LedgerSource.credit_note,
        amount=_money(note.total),
        currency=note.currency or "NGN",
        memo=note.memo or note.credit_number or "Credit note",
        occurred_at=_event_date(note.created_at),
        raw=note,
    )


def _ledger_event(entry: LedgerEntry) -> CustomerFinancialEvent | None:
    if _memo_is_internal(entry.memo):
        return None
    source = entry.source or LedgerSource.other
    return CustomerFinancialEvent(
        id=f"ledger:{entry.id}",
        account_id=entry.account_id,
        entry_type=entry.entry_type,
        source=source,
        amount=_money(entry.amount),
        currency=entry.currency or "NGN",
        memo=entry.memo or source.value.replace("_", " ").title(),
        occurred_at=_event_date(entry.effective_date or entry.created_at),
        raw=entry,
    )


def list_customer_financial_events(
    db: Session,
    account_id: str | UUID,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    currency: str | None = "NGN",
) -> list[CustomerFinancialEvent]:
    account_uuid = coerce_uuid(account_id)
    has_legacy_mirror = _has_legacy_mirror(db, account_uuid)
    events: list[CustomerFinancialEvent] = []

    if has_legacy_mirror:
        legacy_query = (
            db.query(SplynxBillingTransaction)
            .options(
                load_only(
                    SplynxBillingTransaction.id,
                    SplynxBillingTransaction.subscriber_id,
                    SplynxBillingTransaction.entry_type,
                    SplynxBillingTransaction.amount,
                    SplynxBillingTransaction.category_name,
                    SplynxBillingTransaction.description,
                    SplynxBillingTransaction.transaction_date,
                    SplynxBillingTransaction.splynx_invoice_id,
                    SplynxBillingTransaction.splynx_payment_id,
                    SplynxBillingTransaction.splynx_credit_note_id,
                )
            )
            .filter(SplynxBillingTransaction.subscriber_id == account_uuid)
            .filter(SplynxBillingTransaction.deleted.is_(False))
            .filter(SplynxBillingTransaction.transaction_date.isnot(None))
        )
        if start is not None:
            legacy_query = legacy_query.filter(
                SplynxBillingTransaction.transaction_date >= start.date()
            )
        if end is not None:
            legacy_query = legacy_query.filter(
                SplynxBillingTransaction.transaction_date < end.date()
            )
        events.extend(
            event
            for txn in legacy_query.all()
            if (event := _legacy_event(txn)) is not None
        )

    doc_start = PAYMENT_ACTIVITY_AT if has_legacy_mirror else None
    payment_query = (
        db.query(Payment)
        .filter(Payment.account_id == account_uuid)
        .filter(Payment.is_active.is_(True))
        .filter(
            Payment.status.in_(
                [
                    PaymentStatus.succeeded,
                    PaymentStatus.partially_refunded,
                    PaymentStatus.refunded,
                ]
            )
        )
    )
    if currency is not None:
        payment_query = payment_query.filter(Payment.currency == currency)
    if doc_start is not None:
        payment_query = payment_query.filter(Payment.created_at >= doc_start)
    events.extend(
        event for payment in payment_query.all() if (event := _payment_event(payment))
    )

    allocation_query = (
        db.query(PaymentAllocation)
        .join(Invoice, Invoice.id == PaymentAllocation.invoice_id)
        .join(Payment, Payment.id == PaymentAllocation.payment_id)
        .filter(Invoice.account_id == account_uuid)
        .filter(PaymentAllocation.is_active.is_(True))
        .filter(Payment.is_active.is_(True))
        .filter(
            Payment.status.in_(
                [
                    PaymentStatus.succeeded,
                    PaymentStatus.partially_refunded,
                    PaymentStatus.refunded,
                ]
            )
        )
        .filter(or_(Payment.account_id.is_(None), Payment.account_id != account_uuid))
    )
    if currency is not None:
        allocation_query = allocation_query.filter(Payment.currency == currency)
    if doc_start is not None:
        allocation_query = allocation_query.filter(Payment.created_at >= doc_start)
    events.extend(
        event
        for allocation in allocation_query.all()
        if (event := _external_allocation_event(allocation))
    )

    service_start = SERVICE_ACTIVITY_AT if has_legacy_mirror else None
    invoice_query = (
        db.query(Invoice)
        .filter(Invoice.account_id == account_uuid)
        .filter(Invoice.is_active.is_(True))
        .filter(
            Invoice.status.in_(
                [
                    InvoiceStatus.issued,
                    InvoiceStatus.partially_paid,
                    InvoiceStatus.overdue,
                    InvoiceStatus.paid,
                ]
            )
        )
        .filter(Invoice.is_proforma.is_(False))
        .filter(collectible_ar_invoice_filter())
    )
    if currency is not None:
        invoice_query = invoice_query.filter(Invoice.currency == currency)
    if service_start is not None:
        invoice_query = invoice_query.filter(Invoice.created_at >= service_start)
    events.extend(_invoice_event(invoice) for invoice in invoice_query.all())

    credit_note_query = (
        db.query(CreditNote)
        .filter(CreditNote.account_id == account_uuid)
        .filter(CreditNote.is_active.is_(True))
        .filter(
            CreditNote.status.in_(
                [
                    CreditNoteStatus.issued,
                    CreditNoteStatus.partially_applied,
                    CreditNoteStatus.applied,
                ]
            )
        )
    )
    if currency is not None:
        credit_note_query = credit_note_query.filter(CreditNote.currency == currency)
    if service_start is not None:
        credit_note_query = credit_note_query.filter(
            CreditNote.created_at >= service_start
        )
    events.extend(_credit_note_event(note) for note in credit_note_query.all())

    ledger_query = (
        db.query(LedgerEntry)
        .filter(LedgerEntry.account_id == account_uuid)
        .filter(LedgerEntry.invoice_id.is_(None))
        .filter(LedgerEntry.is_active.is_(True))
        .filter(
            or_(
                LedgerEntry.source.in_([LedgerSource.adjustment, LedgerSource.other]),
                # A refund entry that carries a payment_id is the LEDGER's
                # representation of a refund this reader has already counted on the
                # Payment document, via ``net_amount = amount - refunded_amount``
                # (see _payment_event). Counting both subtracted the refund TWICE:
                # refunding NGN X dropped the customer's available balance by NGN 2X,
                # which on a prepaid account is a false under-funding and a wrongful
                # suspension of someone we had just refunded.
                #
                # This mirrors the payment clause immediately below, which has always
                # required payment_id IS NULL for exactly the same reason. A refund
                # NOT linked to a payment (a bare ledger adjustment) is still counted.
                and_(
                    LedgerEntry.source == LedgerSource.refund,
                    LedgerEntry.payment_id.is_(None),
                ),
                and_(
                    LedgerEntry.source == LedgerSource.invoice,
                    LedgerEntry.entry_type == LedgerEntryType.debit,
                ),
                and_(
                    LedgerEntry.source == LedgerSource.payment,
                    LedgerEntry.payment_id.is_(None),
                ),
                and_(
                    LedgerEntry.source == LedgerSource.credit_note,
                    LedgerEntry.payment_id.is_(None),
                ),
            )
        )
    )
    if currency is not None:
        ledger_query = ledger_query.filter(LedgerEntry.currency == currency)
    if has_legacy_mirror:
        ledger_event_date = func.coalesce(
            LedgerEntry.effective_date, LedgerEntry.created_at
        )
        ledger_query = ledger_query.filter(
            ledger_event_date > LEGACY_LEDGER_CUTOVER,
        )
    events.extend(
        event for entry in ledger_query.all() if (event := _ledger_event(entry))
    )

    return sorted(
        (
            event
            for event in events
            if _in_window(event.occurred_at, start=start, end=end)
        ),
        key=lambda event: (event.occurred_at, event.id),
    )


def customer_financial_balances_by_currency(
    db: Session,
    account_ids: list[str | UUID] | tuple[str | UUID, ...] | set[str | UUID],
) -> dict[UUID, dict[str, Decimal]]:
    """Aggregate canonical balances for a cohort without materializing events."""
    account_uuids = sorted(
        {coerce_uuid(account_id) for account_id in account_ids}, key=str
    )
    if not account_uuids:
        return {}

    balances: dict[UUID, dict[str, Decimal]] = {
        account_id: {} for account_id in account_uuids
    }
    legacy_account_ids = set(
        row[0]
        for row in db.query(SplynxBillingTransaction.subscriber_id)
        .filter(SplynxBillingTransaction.subscriber_id.in_(account_uuids))
        .filter(SplynxBillingTransaction.deleted.is_(False))
        .distinct()
        .all()
    )

    def add(rows) -> None:  # noqa: ANN001
        for account_id, currency, amount in rows:
            code = str(currency or "NGN")
            account_balances = balances[account_id]
            account_balances[code] = account_balances.get(
                code, Decimal("0.00")
            ) + round_money(Decimal(str(amount or 0)))

    if legacy_account_ids:
        legacy_signed = case(
            (
                SplynxBillingTransaction.entry_type == LedgerEntryType.credit.value,
                SplynxBillingTransaction.amount,
            ),
            else_=-SplynxBillingTransaction.amount,
        )
        add(
            db.query(
                SplynxBillingTransaction.subscriber_id,
                literal("NGN").label("currency"),
                func.sum(legacy_signed).label("balance"),
            )
            .filter(SplynxBillingTransaction.subscriber_id.in_(legacy_account_ids))
            .filter(SplynxBillingTransaction.deleted.is_(False))
            .filter(SplynxBillingTransaction.transaction_date.isnot(None))
            .group_by(SplynxBillingTransaction.subscriber_id)
            .all()
        )

    payment_currency = func.coalesce(Payment.currency, "NGN")
    payment_net = func.coalesce(Payment.amount, 0) - func.coalesce(
        Payment.refunded_amount, 0
    )
    payment_query = (
        db.query(
            Payment.account_id,
            payment_currency.label("currency"),
            func.sum(payment_net).label("balance"),
        )
        .filter(Payment.account_id.in_(account_uuids))
        .filter(Payment.is_active.is_(True))
        .filter(
            Payment.status.in_(
                [
                    PaymentStatus.succeeded,
                    PaymentStatus.partially_refunded,
                    PaymentStatus.refunded,
                ]
            )
        )
        .filter(payment_net > 0)
    )
    if legacy_account_ids:
        payment_query = payment_query.filter(
            or_(
                Payment.account_id.notin_(legacy_account_ids),
                Payment.created_at >= PAYMENT_ACTIVITY_AT,
            )
        )
    add(payment_query.group_by(Payment.account_id, payment_currency).all())

    allocation_currency = func.coalesce(Payment.currency, Invoice.currency, "NGN")
    allocation_query = (
        db.query(
            Invoice.account_id,
            allocation_currency.label("currency"),
            func.sum(PaymentAllocation.amount).label("balance"),
        )
        .join(Invoice, Invoice.id == PaymentAllocation.invoice_id)
        .join(Payment, Payment.id == PaymentAllocation.payment_id)
        .filter(Invoice.account_id.in_(account_uuids))
        .filter(PaymentAllocation.is_active.is_(True))
        .filter(Payment.is_active.is_(True))
        .filter(
            Payment.status.in_(
                [
                    PaymentStatus.succeeded,
                    PaymentStatus.partially_refunded,
                    PaymentStatus.refunded,
                ]
            )
        )
        .filter(
            or_(Payment.account_id.is_(None), Payment.account_id != Invoice.account_id)
        )
    )
    if legacy_account_ids:
        allocation_query = allocation_query.filter(
            or_(
                Invoice.account_id.notin_(legacy_account_ids),
                Payment.created_at >= PAYMENT_ACTIVITY_AT,
            )
        )
    add(allocation_query.group_by(Invoice.account_id, allocation_currency).all())

    invoice_currency = func.coalesce(Invoice.currency, "NGN")
    invoice_query = (
        db.query(
            Invoice.account_id,
            invoice_currency.label("currency"),
            (-func.sum(Invoice.total)).label("balance"),
        )
        .filter(Invoice.account_id.in_(account_uuids))
        .filter(Invoice.is_active.is_(True))
        .filter(
            Invoice.status.in_(
                [
                    InvoiceStatus.issued,
                    InvoiceStatus.partially_paid,
                    InvoiceStatus.overdue,
                    InvoiceStatus.paid,
                ]
            )
        )
        .filter(Invoice.is_proforma.is_(False))
        .filter(collectible_ar_invoice_filter())
    )
    if legacy_account_ids:
        invoice_query = invoice_query.filter(
            or_(
                Invoice.account_id.notin_(legacy_account_ids),
                Invoice.created_at >= SERVICE_ACTIVITY_AT,
            )
        )
    add(invoice_query.group_by(Invoice.account_id, invoice_currency).all())

    note_currency = func.coalesce(CreditNote.currency, "NGN")
    note_query = (
        db.query(
            CreditNote.account_id,
            note_currency.label("currency"),
            func.sum(CreditNote.total).label("balance"),
        )
        .filter(CreditNote.account_id.in_(account_uuids))
        .filter(CreditNote.is_active.is_(True))
        .filter(
            CreditNote.status.in_(
                [
                    CreditNoteStatus.issued,
                    CreditNoteStatus.partially_applied,
                    CreditNoteStatus.applied,
                ]
            )
        )
    )
    if legacy_account_ids:
        note_query = note_query.filter(
            or_(
                CreditNote.account_id.notin_(legacy_account_ids),
                CreditNote.created_at >= SERVICE_ACTIVITY_AT,
            )
        )
    add(note_query.group_by(CreditNote.account_id, note_currency).all())

    ledger_currency = func.coalesce(LedgerEntry.currency, "NGN")
    ledger_signed = case(
        (LedgerEntry.entry_type == LedgerEntryType.credit, LedgerEntry.amount),
        else_=-LedgerEntry.amount,
    )
    ledger_query = (
        db.query(
            LedgerEntry.account_id,
            ledger_currency.label("currency"),
            func.sum(ledger_signed).label("balance"),
        )
        .filter(LedgerEntry.account_id.in_(account_uuids))
        .filter(LedgerEntry.invoice_id.is_(None))
        .filter(LedgerEntry.is_active.is_(True))
        .filter(
            or_(
                LedgerEntry.source.in_([LedgerSource.adjustment, LedgerSource.other]),
                and_(
                    LedgerEntry.source == LedgerSource.refund,
                    LedgerEntry.payment_id.is_(None),
                ),
                and_(
                    LedgerEntry.source == LedgerSource.invoice,
                    LedgerEntry.entry_type == LedgerEntryType.debit,
                ),
                and_(
                    LedgerEntry.source == LedgerSource.payment,
                    LedgerEntry.payment_id.is_(None),
                ),
                and_(
                    LedgerEntry.source == LedgerSource.credit_note,
                    LedgerEntry.payment_id.is_(None),
                ),
            )
        )
    )
    memo = func.coalesce(LedgerEntry.memo, "")
    ledger_query = ledger_query.filter(memo.notin_(INTERNAL_MEMO_EXACT))
    for prefix in INTERNAL_MEMO_PREFIXES:
        ledger_query = ledger_query.filter(~memo.startswith(prefix))
    if legacy_account_ids:
        ledger_event_date = func.coalesce(
            LedgerEntry.effective_date, LedgerEntry.created_at
        )
        ledger_query = ledger_query.filter(
            or_(
                LedgerEntry.account_id.notin_(legacy_account_ids),
                ledger_event_date > LEGACY_LEDGER_CUTOVER,
            )
        )
    add(ledger_query.group_by(LedgerEntry.account_id, ledger_currency).all())

    return balances


def calculate_customer_balance(
    db: Session, account_id: str | UUID, *, currency: str | None = "NGN"
) -> Decimal:
    return round_money(
        sum(
            (
                event.signed_amount
                for event in list_customer_financial_events(
                    db, account_id, currency=currency
                )
            ),
            Decimal("0.00"),
        )
    )
