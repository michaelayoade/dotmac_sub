"""Reconcile cutover payments that posted as credit but never settled debt.

Background
----------
During the legacy-BSS -> local-ledger cutover (from 2026-06-13) a cohort of
``succeeded`` gateway payments landed without their balance being applied to the
customer's open invoices. Two distinct failure modes produced the same visible
symptom (*"paid but still walled-garden"*):

1. **Reseller-consolidated payments** lost ``billing_account_id`` in the webhook
   ingest path (see ``providers.PaymentProviderEvents.ingest``), so the surplus
   never incremented ``BillingAccount.balance`` and never settled invoices.
2. **Native subscriber payments** that were captured *before* the period's
   invoice was issued: the top-up auto-allocated to nothing and parked as
   unallocated account credit, then the new invoice issued afterwards and the
   credit just sat there while the debt aged the subscriber into suspension.

The durable fix is to take each account's *available balance* (unallocated
payment credit) and apply it to the account's open invoices — settling the debt
exactly as if the money had been posted at invoice time — then let the existing
``restore_account_services`` chain recompute status and lift enforcement.

This module is the money engine. It reuses the canonical allocation primitives
(``_apply_payment_allocation`` / ``_recalculate_invoice_totals``) so the
bookkeeping is byte-for-byte identical to ``Payments.create``'s auto-allocate
path — the same path that already works on the happy path. It is invoked by the
one-off CLI ``scripts/one_off/reconcile_unposted_payments.py`` (dry-run first)
and is safe to re-run: it is idempotent per (payment, invoice).
"""

from __future__ import annotations

import logging
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.billing import (
    CreditNoteApplication,
    Invoice,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentAllocation,
    PaymentStatus,
)
from app.services.billing._common import (
    _recalculate_invoice_totals as recalculate_invoice_totals,
)
from app.services.billing._common import (
    get_account_credit_balance,
    lock_account,
)
from app.services.billing.payments import _apply_payment_allocation
from app.services.common import coerce_uuid, round_money, to_decimal
from app.services.notification_suppression import suppress_notifications

logger = logging.getLogger(__name__)

# Memo stamped on the offsetting unallocated debit so the reduction of the
# credit pool is auditable and distinguishable from refunds.
CREDIT_SETTLEMENT_MEMO = (
    "Available balance applied to open invoices (cutover reconcile)"
)

_OPEN_STATUSES = (
    InvoiceStatus.issued,
    InvoiceStatus.partially_paid,
    InvoiceStatus.overdue,
)


@dataclass
class SettleResult:
    """Outcome of settling one account's open invoices from its credit."""

    account_id: str
    available_credit: Decimal = Decimal("0.00")
    applied: Decimal = Decimal("0.00")
    invoices_settled: list[str] = field(default_factory=list)
    invoices_touched: list[str] = field(default_factory=list)
    # Credit that exists but is not backed by an allocatable succeeded payment
    # (e.g. it came from an adjustment/credit-note rather than a payment). We do
    # NOT fabricate a backing payment to spend it — that is exactly the kind of
    # re-derivation that produced the phantom-invoice divergence — so it is left
    # untouched and surfaced here for a human to look at.
    unbacked_credit: Decimal = Decimal("0.00")

    @property
    def changed(self) -> bool:
        return self.applied > 0


def _open_invoices(db: Session, account_id: str) -> list[Invoice]:
    """Account's open invoices, oldest-due first (matches _auto_allocate order)."""
    return (
        db.query(Invoice)
        .filter(Invoice.account_id == coerce_uuid(account_id))
        .filter(Invoice.is_active.is_(True))
        .filter(Invoice.status.in_(_OPEN_STATUSES))
        .filter(Invoice.balance_due > 0)
        .order_by(Invoice.due_at.asc().nulls_last(), Invoice.created_at.asc())
        .all()
    )


def _allocatable_payments(
    db: Session, account_id: str
) -> list[tuple[Payment, Decimal]]:
    """Succeeded payments for the account with their already-allocated totals.

    Returns ``(payment, unallocated_room)`` oldest first. ``unallocated_room`` is
    ``amount - Σ active allocations`` — the part of the payment still available
    to apply to an invoice (so a re-run naturally skips fully-allocated rows).
    """
    allocated_sq = (
        db.query(
            PaymentAllocation.payment_id.label("payment_id"),
            func.coalesce(func.sum(PaymentAllocation.amount), Decimal("0.00")).label(
                "allocated"
            ),
        )
        .filter(PaymentAllocation.is_active.is_(True))
        .group_by(PaymentAllocation.payment_id)
        .subquery()
    )
    rows = (
        db.query(
            Payment,
            func.coalesce(allocated_sq.c.allocated, Decimal("0.00")).label("allocated"),
        )
        .outerjoin(allocated_sq, allocated_sq.c.payment_id == Payment.id)
        .filter(Payment.account_id == coerce_uuid(account_id))
        .filter(Payment.is_active.is_(True))
        .filter(Payment.status == PaymentStatus.succeeded)
        .order_by(Payment.paid_at.asc().nulls_last(), Payment.created_at.asc())
        .all()
    )
    out: list[tuple[Payment, Decimal]] = []
    for payment, allocated in rows:
        room = round_money(to_decimal(payment.amount) - to_decimal(allocated))
        if room > 0:
            out.append((payment, room))
    return out


def settle_open_invoices_from_credit(db: Session, account_id: str) -> SettleResult:
    """Apply an account's available (unallocated) credit to its open invoices.

    The credit is consumed from the account's existing succeeded payments,
    oldest first, and applied to the oldest open invoices. For every naira
    applied we (a) create the ``PaymentAllocation`` (which reduces the invoice's
    ``balance_due`` on recalculation) and (b) write one offsetting unallocated
    **debit** ledger entry, so ``get_account_credit_balance`` drops by exactly
    the applied amount and the money is never double-counted.

    Does NOT commit — the caller owns the transaction boundary (so the one-off
    can commit per account and roll back a single bad account without losing the
    batch). Returns a :class:`SettleResult`.
    """
    result = SettleResult(account_id=str(account_id))

    # Serialize the read-modify-write of the credit pool for this account.
    lock_account(db, str(account_id))

    invoices = _open_invoices(db, str(account_id))
    if not invoices:
        return result

    currencies = sorted({invoice.currency or "NGN" for invoice in invoices})
    credit_by_currency = {
        currency: get_account_credit_balance(db, str(account_id), currency=currency)
        for currency in currencies
    }
    result.available_credit = round_money(
        sum(
            (
                credit
                for credit in credit_by_currency.values()
                if credit > Decimal("0.00")
            ),
            Decimal("0.00"),
        )
    )
    if result.available_credit <= 0:
        return result

    payments = _allocatable_payments(db, str(account_id))
    payment_backed_by_currency: dict[str, Decimal] = {}
    for payment, room in payments:
        currency = payment.currency or "NGN"
        payment_backed_by_currency[currency] = round_money(
            payment_backed_by_currency.get(currency, Decimal("0.00")) + room
        )
    payment_backed = round_money(
        sum((room for _payment, room in payments), Decimal("0.00"))
    )
    # Spend only credit that real succeeded payments can back. Any surplus credit
    # with no allocatable payment behind it is left alone and reported.
    spendable_by_currency = {
        currency: min(
            max(credit_by_currency.get(currency, Decimal("0.00")), Decimal("0.00")),
            payment_backed_by_currency.get(currency, Decimal("0.00")),
        )
        for currency in currencies
    }
    result.unbacked_credit = round_money(
        sum(
            (
                max(
                    credit_by_currency.get(currency, Decimal("0.00"))
                    - payment_backed_by_currency.get(currency, Decimal("0.00")),
                    Decimal("0.00"),
                )
                for currency in currencies
            ),
            Decimal("0.00"),
        )
    )

    remaining_by_currency = dict(spendable_by_currency)
    applied_by_currency: dict[str, Decimal] = {}
    room_by_payment: dict = {payment.id: room for payment, room in payments}
    touched: set = set()

    for invoice in invoices:
        currency = invoice.currency or "NGN"
        remaining = remaining_by_currency.get(currency, Decimal("0.00"))
        if remaining <= 0:
            continue
        invoice_remaining = _project_invoice_remaining(db, invoice)
        if invoice_remaining <= 0:
            if to_decimal(invoice.balance_due) > 0:
                touched.add(invoice.id)
            continue
        for payment, _room in payments:
            if remaining <= 0 or invoice_remaining <= 0:
                break
            if payment.currency != invoice.currency:
                continue
            payment_room = room_by_payment.get(payment.id, Decimal("0.00"))
            if payment_room <= 0:
                continue
            amount = min(remaining, invoice_remaining, payment_room)
            if amount <= 0:
                continue
            _allocation, applied = _apply_payment_allocation(
                db,
                payment,
                invoice,
                amount,
                memo="Available balance applied (cutover reconcile)",
            )
            result.applied = round_money(result.applied + applied)
            remaining = round_money(remaining - applied)
            remaining_by_currency[currency] = remaining
            applied_by_currency[currency] = round_money(
                applied_by_currency.get(currency, Decimal("0.00")) + applied
            )
            invoice_remaining = round_money(invoice_remaining - applied)
            room_by_payment[payment.id] = round_money(payment_room - applied)
            touched.add(invoice.id)

    if result.applied <= 0 and not touched:
        return result

    db.flush()
    for invoice_id in touched:
        inv = db.get(Invoice, invoice_id)
        if inv is None:
            continue
        recalculate_invoice_totals(db, inv)
        result.invoices_touched.append(str(invoice_id))
        if inv.status == InvoiceStatus.paid:
            result.invoices_settled.append(str(invoice_id))

    if result.applied > 0:
        # Reduce the unallocated-credit pool by exactly what we applied. Mirrors
        # the reseller path's closing ``BillingAccounts.debit_balance``; here
        # the pool is the per-account ledger, so the reduction is a debit with
        # no invoice_id.
        for currency, applied in applied_by_currency.items():
            if applied <= 0:
                continue
            db.add(
                LedgerEntry(
                    account_id=coerce_uuid(account_id),
                    invoice_id=None,
                    payment_id=None,
                    entry_type=LedgerEntryType.debit,
                    source=LedgerSource.payment,
                    amount=applied,
                    currency=currency,
                    memo=CREDIT_SETTLEMENT_MEMO,
                )
            )
    db.flush()
    logger.info(
        "Cutover reconcile: applied %s credit to %d invoice(s) for account %s "
        "(%d fully settled)",
        result.applied,
        len(result.invoices_touched),
        account_id,
        len(result.invoices_settled),
    )
    return result


def project_settlement(db: Session, account_id: str) -> SettleResult:
    """Read-only projection of :func:`settle_open_invoices_from_credit`.

    Computes how much credit *would* be applied and which invoices *would*
    settle, writing nothing. Used by the one-off CLI's dry-run so a dry-run is
    guaranteed side-effect free (never relies on a write-then-rollback).
    """
    result = SettleResult(account_id=str(account_id))
    invoices = _open_invoices(db, str(account_id))
    if not invoices:
        return result

    currencies = sorted({invoice.currency or "NGN" for invoice in invoices})
    credit_by_currency = {
        currency: get_account_credit_balance(db, str(account_id), currency=currency)
        for currency in currencies
    }
    result.available_credit = round_money(
        sum(
            (
                credit
                for credit in credit_by_currency.values()
                if credit > Decimal("0.00")
            ),
            Decimal("0.00"),
        )
    )
    if result.available_credit <= 0:
        return result

    payments = _allocatable_payments(db, str(account_id))
    payment_backed_by_currency: dict[str, Decimal] = {}
    for payment, room in payments:
        currency = payment.currency or "NGN"
        payment_backed_by_currency[currency] = round_money(
            payment_backed_by_currency.get(currency, Decimal("0.00")) + room
        )
    payment_backed = round_money(
        sum((room for _payment, room in payments), Decimal("0.00"))
    )
    remaining_by_currency = {
        currency: min(
            max(credit_by_currency.get(currency, Decimal("0.00")), Decimal("0.00")),
            payment_backed_by_currency.get(currency, Decimal("0.00")),
        )
        for currency in currencies
    }
    result.unbacked_credit = round_money(
        sum(
            (
                max(
                    credit_by_currency.get(currency, Decimal("0.00"))
                    - payment_backed_by_currency.get(currency, Decimal("0.00")),
                    Decimal("0.00"),
                )
                for currency in currencies
            ),
            Decimal("0.00"),
        )
    )
    _ = payment_backed
    for invoice in invoices:
        currency = invoice.currency or "NGN"
        remaining = remaining_by_currency.get(currency, Decimal("0.00"))
        if remaining <= 0:
            continue
        invoice_remaining = _project_invoice_remaining(db, invoice)
        if invoice_remaining <= 0:
            continue
        apply_here = min(remaining, invoice_remaining)
        if apply_here <= 0:
            continue
        result.applied = round_money(result.applied + apply_here)
        remaining = round_money(remaining - apply_here)
        remaining_by_currency[currency] = remaining
        result.invoices_touched.append(str(invoice.id))
        if apply_here >= invoice_remaining:
            result.invoices_settled.append(str(invoice.id))
    return result


def _project_invoice_remaining(db: Session, invoice: Invoice) -> Decimal:
    allocated = (
        db.query(func.coalesce(func.sum(PaymentAllocation.amount), 0))
        .join(Payment, Payment.id == PaymentAllocation.payment_id)
        .filter(PaymentAllocation.invoice_id == invoice.id)
        .filter(PaymentAllocation.is_active.is_(True))
        .filter(Payment.is_active.is_(True))
        .filter(Payment.status == PaymentStatus.succeeded)
        .scalar()
    )
    credited = (
        db.query(func.coalesce(func.sum(CreditNoteApplication.amount), 0))
        .filter(CreditNoteApplication.invoice_id == invoice.id)
        .scalar()
    )
    computed_remaining = round_money(
        to_decimal(invoice.total) - to_decimal(allocated) - to_decimal(credited)
    )
    return max(
        Decimal("0.00"), min(to_decimal(invoice.balance_due), computed_remaining)
    )


@dataclass
class ReconcileResult:
    """Outcome of reconciling one subscriber (money + queued service restore)."""

    account_id: str
    settle: SettleResult
    subscriptions_restored: int = 0
    subscription_ids: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def changed(self) -> bool:
        return self.settle.changed or self.subscriptions_restored > 0


def _account_subscription_ids(db: Session, account_id: str) -> list[str]:
    from app.models.catalog import Subscription

    rows = (
        db.query(Subscription.id)
        .filter(Subscription.subscriber_id == coerce_uuid(account_id))
        .all()
    )
    return [str(r[0]) for r in rows]


def reconcile_subscriber(
    db: Session, account_id: str, *, dry_run: bool = True
) -> ReconcileResult:
    """Settle one account's debt from available balance and restore its service.

    Dry-run returns a read-only projection. Apply mode mutates and **commits**
    on success (so the caller's batch survives a single bad account) or rolls
    back and records the error. RADIUS refresh + CoA are handled once, at the
    batch level, by :func:`reconcile_cohort` after the money pass.
    """
    if dry_run:
        return ReconcileResult(
            account_id=str(account_id),
            settle=project_settlement(db, str(account_id)),
            subscription_ids=_account_subscription_ids(db, str(account_id)),
        )

    from app.services import collections as collections_service

    result = ReconcileResult(
        account_id=str(account_id),
        settle=SettleResult(account_id=str(account_id)),
    )
    try:
        result.settle = settle_open_invoices_from_credit(db, str(account_id))
        # Restore service for each invoice we fully settled. restore_account_services
        # is idempotent (it only acts on still-suspended subscriptions / open dunning
        # cases), so it is safe to call once per settled invoice.
        seen: set = set()
        for invoice_id in result.settle.invoices_settled:
            restored = collections_service.restore_account_services(
                db, str(account_id), invoice_id=invoice_id
            )
            result.subscriptions_restored += restored
            seen.add(invoice_id)
        # Even when nothing was newly settled, run a no-invoice restore so an
        # account that already cleared its debt (credit fully covers it) but is
        # still suspended gets re-evaluated.
        if (
            not result.settle.invoices_settled
            and not collections_service.has_overdue_balance(db, str(account_id))
        ):
            result.subscriptions_restored += (
                collections_service.restore_account_services(db, str(account_id))
            )
        result.subscription_ids = _account_subscription_ids(db, str(account_id))
        db.commit()
    except Exception as exc:  # noqa: BLE001 — isolate one bad account from the batch
        db.rollback()
        result.error = str(exc)
        logger.exception("Cutover reconcile failed for account %s", account_id)
    return result


def find_cohort_account_ids(
    db: Session, *, since: datetime | None = None, limit: int | None = None
) -> list[str]:
    """Accounts that hold unallocated credit AND still carry open invoice debt —
    money sitting while debt ages ('paid but unsettled').

    Keyed on credit + open-debt state, NOT on when a payment landed (the credit
    can predate any given date) nor on ``billing_account_id IS NULL`` (normal for
    native subscribers). ``since``, when given, additionally scopes candidates to
    accounts with a succeeded payment on/after that date — useful to target a
    specific window, but it UNDER-counts the true cohort, so the default (None)
    is the full set. The settler only ever applies payment-backed credit, so
    deposit-seed / credit-note balances surface as ``unbacked_credit`` rather
    than being settled.
    """
    # Necessary condition: an open invoice with a positive balance. This bounds
    # the candidate set without depending on when any payment arrived.
    candidate_ids = [
        str(r[0])
        for r in (
            db.query(Invoice.account_id)
            .filter(Invoice.account_id.isnot(None))
            .filter(Invoice.is_active.is_(True))
            .filter(Invoice.status.in_(_OPEN_STATUSES))
            .filter(Invoice.balance_due > 0)
            .distinct()
            .all()
        )
    ]
    if since is not None:
        paid_since = {
            str(r[0])
            for r in (
                db.query(Payment.account_id)
                .filter(Payment.account_id.isnot(None))
                .filter(Payment.is_active.is_(True))
                .filter(Payment.status == PaymentStatus.succeeded)
                .filter(Payment.created_at >= since)
                .distinct()
                .all()
            )
        }
        candidate_ids = [a for a in candidate_ids if a in paid_since]

    out: list[str] = []
    for account_id in candidate_ids:
        if get_account_credit_balance(db, account_id) <= 0:
            continue
        out.append(account_id)
        if limit is not None and len(out) >= limit:
            break
    return out


def reconcile_cohort(
    db: Session,
    *,
    since: datetime | None = None,
    limit: int | None = None,
    dry_run: bool = True,
    refresh_radius: bool = True,
    send_coa: bool = True,
    extra_subscription_ids: list[str] | None = None,
    notify: bool = False,
) -> dict:
    """Drive the full cutover reconcile: settle the cohort's debt from balance,
    then (apply only) rebuild RADIUS once and CoA-kick the affected sessions.

    ``extra_subscription_ids`` lets an operator force the RADIUS refresh + CoA
    onto specific reported logins that have no remaining debt but a stale
    walled-garden tag (the 'paid + active but still walled' cohort).

    ``notify`` is False by default: this is bookkeeping catch-up over old,
    already-arrived funds across a mostly-churned cohort, so customer
    notifications ("Payment received" / "Service resumed") are suppressed — they
    would be factually wrong and a bulk burst would damage sender reputation.
    Set notify=True only for a live, customer-initiated reconcile.
    """
    account_ids = find_cohort_account_ids(db, since=since, limit=limit)

    suppress_ctx = nullcontext() if (notify or dry_run) else suppress_notifications()
    with suppress_ctx:
        results = [
            reconcile_subscriber(db, aid, dry_run=dry_run) for aid in account_ids
        ]

    total_applied = round_money(
        sum((r.settle.applied for r in results), Decimal("0.00"))
    )
    changed = [r for r in results if r.changed]
    errors = [r for r in results if r.error]

    # Subscriptions to kick: every subscription under a changed account, plus any
    # explicitly-supplied reported logins.
    coa_subscription_ids: set = set(extra_subscription_ids or [])
    for r in changed:
        coa_subscription_ids.update(r.subscription_ids)

    summary = {
        "since": since.isoformat() if since is not None else None,
        "candidates": len(account_ids),
        "accounts_changed": len(changed),
        "total_applied": str(total_applied),
        "invoices_settled": sum(len(r.settle.invoices_settled) for r in results),
        "subscriptions_restored": sum(r.subscriptions_restored for r in results),
        "errors": len(errors),
        "unbacked_credit_accounts": sum(
            1 for r in results if r.settle.unbacked_credit > 0
        ),
        "dry_run": dry_run,
        "radius_refreshed": False,
        "sessions_kicked": 0,
        "results": results,
    }

    if dry_run:
        return summary

    if refresh_radius:
        # Whole-table, idempotent rebuild of radcheck/radreply from current
        # subscription/subscriber status. This is what lifts the stale
        # walled-garden tag for the 'paid + active but still walled' accounts,
        # not just the ones whose debt we just settled.
        from app.services.radius_population import populate

        populate(dry_run=False)
        summary["radius_refreshed"] = True

    if send_coa and coa_subscription_ids:
        from app.services.enforcement import disconnect_subscription_sessions

        kicked = 0
        for subscription_id in coa_subscription_ids:
            try:
                kicked += disconnect_subscription_sessions(
                    db, subscription_id, reason="cutover payment reconcile"
                )
            except Exception:
                logger.warning(
                    "Cutover reconcile: CoA kick failed for subscription %s",
                    subscription_id,
                    exc_info=True,
                )
        summary["sessions_kicked"] = kicked

    return summary
