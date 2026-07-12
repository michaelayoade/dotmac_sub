"""Read-only reconciliation pass over every billing discrepancy class.

Companion to ``docs/audits/BILLING_SOT_AUDIT_2026-07-12.md``. This script does
not write. It has no ``--apply`` flag and never commits: the session is rolled
back in a ``finally`` block. Its job is to size each discrepancy class on real
data so the historical-repair scope can be set before any correction is built.

The audit's "possible impact" caveat is the whole point of this tool. Every
finding in the report is a reachable code path; only a query against real data
says whether it actually fired, and how often, and for how much money.

Classes reported
----------------
  D1  ledger double-swing        F1   reversal posted AND original deactivated
  D2  unbacked dead credit       F1   deactivated credit, no Payment doc behind it
  D3  paid-with-balance          F24  invoice paid yet balance_due > 0
  D4  native orphan payments     F19  succeeded native payment, no allocation, no ledger
  D5  misallocated payments      F3   ledger invoice_id != allocation invoice_id
  D6  succeeded, no paid_at      F15  blinds the enforcement health gate
  D7  balance-definition split   F4   ledger credit vs document position disagree in SIGN
  D8  unapplied credit notes     F4   spendable per documents, invisible to the ledger
  D9  pending money              F18  pending payment already holding allocation/ledger
  D10 void with live debits      F6   void bypassed the ledger reversal
  D11 opening debits             --   cutover seed cohort, split by deposit sign
  D12 enforcement mismatch       F6/F7 suspended-but-funded / unfunded-but-active

Two detectors were WRONG on first contact with real data and were narrowed. Both
mistakes were of the same kind — counting a deliberate design decision as damage:

  D2  counted every deactivated ledger row as a silent balance move. Most of them
      duplicate the Payment/Invoice documents the balance is actually derived
      from, so deactivating them removed a double-count. Gross figure was
      NGN 2.2bn; the part that needs adjudication is far smaller.
  D4  counted splynx-imported payments as orphans. They have no local allocation
      or ledger entry BY DESIGN (mirrored, not posted). Gross was 3,117 payments
      / NGN 146M; the real native signal was 1 payment.

Read every future number here the same way: a cluster being real is not the same
as its cause being real.

Examples
--------
  # PostgreSQL replicas are accepted by default; primaries are refused.
  python -m scripts.one_off.billing_alignment_audit --only D2,D7,D12
  python -m scripts.one_off.billing_alignment_audit --out /tmp/align --limit 0
  # Staging primary only, after the host has been explicitly approved:
  python -m scripts.one_off.billing_alignment_audit --only D1,D3 --allow-primary

Production safety
-----------------
The runner sets the PostgreSQL transaction read-only, applies a per-statement
timeout, refuses primaries by default, and derives D7/D12 balances in bounded
batches. ``--allow-primary`` is an explicit exception, not the normal prod path.
Free-text memos are used only inside the detector and are never written to CSV.
"""

from __future__ import annotations

import argparse
import csv
import re
import traceback
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy import true as sa_true
from sqlalchemy.orm import Session

from app.db import SessionLocal
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
from app.models.subscriber import Subscriber

ZERO = Decimal("0.00")
_REVERSAL_OF = re.compile(
    r"Reversal of ledger entry\s+([0-9a-fA-F-]{36})", re.IGNORECASE
)


@dataclass
class Finding:
    """One discrepancy class: its rows, and the money it represents."""

    code: str
    title: str
    ref: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    amount: Decimal = ZERO
    note: str = ""
    error: str = ""

    @property
    def count(self) -> int:
        return len(self.rows)


def _money(v: object) -> Decimal:
    if v is None:
        return ZERO
    return Decimal(str(v))


def _account_filter(column, account_ids):
    """Restrict to a set of accounts, or scan the whole table when None.

    A chunked ``IN (...)`` list of a few hundred UUIDs makes the planner scan
    ledger_entries once PER CHUNK and blows the statement timeout. One
    whole-table GROUP BY aggregate over the same rows is a single sequential
    scan (measured on staging: 212k rows, 3.8s, well inside the 10s timeout),
    and it answers for every account at once. Pass None to get that.
    """
    if account_ids is None:
        return sa_true()
    return column.in_(account_ids)


def _chunks(values: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _before(value: datetime | None, cutoff: datetime) -> bool:
    comparable = _aware(value)
    return comparable is not None and comparable < cutoff


def _add_position(
    positions: dict[tuple[str, str], Decimal],
    account_id: object,
    currency: str | None,
    amount: object,
    *,
    credit: bool,
) -> None:
    key = (str(account_id), currency or "NGN")
    signed = _money(amount) if credit else -_money(amount)
    positions[key] = positions.get(key, ZERO) + signed


def _batch_customer_positions(
    db: Session,
    account_ids: Sequence[Any],
    *,
    currency: str | None,
) -> dict[tuple[str, str], Decimal]:
    """Derive canonical customer positions in a fixed number of queries.

    This is the batch equivalent of ``list_customer_financial_events``. It is
    intentionally kept in the audit harness rather than made a second runtime
    balance owner. Tests compare it with the canonical per-account function.
    """
    from app.models.splynx_transaction import SplynxBillingTransaction
    from app.services.billing.invoice_classification import (
        collectible_ar_invoice_filter,
    )
    from app.services.customer_financial_ledger import (
        INTERNAL_MEMO_EXACT,
        INTERNAL_MEMO_PREFIXES,
        LEGACY_LEDGER_CUTOVER,
        PAYMENT_ACTIVITY_AT,
        SERVICE_ACTIVITY_AT,
    )

    # account_ids=None means "every account" (one whole-table aggregate). Only an
    # explicitly EMPTY set means "nothing to do".
    if account_ids is not None and not account_ids:
        return {}

    positions: dict[tuple[str, str], Decimal] = {}
    mirror_accounts = {
        str(value)
        for value in db.scalars(
            select(SplynxBillingTransaction.subscriber_id)
            .where(
                _account_filter(SplynxBillingTransaction.subscriber_id, account_ids),
                SplynxBillingTransaction.deleted.is_(False),
            )
            .distinct()
        ).all()
        if value is not None
    }

    if mirror_accounts and currency in (None, "NGN"):
        legacy_rows = db.execute(
            select(
                SplynxBillingTransaction.subscriber_id,
                SplynxBillingTransaction.entry_type,
                SplynxBillingTransaction.amount,
            ).where(
                _account_filter(SplynxBillingTransaction.subscriber_id, account_ids),
                SplynxBillingTransaction.deleted.is_(False),
                SplynxBillingTransaction.transaction_date.isnot(None),
            )
        ).all()
        for legacy_row in legacy_rows:
            _add_position(
                positions,
                legacy_row.subscriber_id,
                "NGN",
                legacy_row.amount,
                credit=str(legacy_row.entry_type) == LedgerEntryType.credit.value,
            )

    payment_stmt = select(
        Payment.account_id,
        Payment.currency,
        Payment.amount,
        Payment.refunded_amount,
        Payment.created_at,
    ).where(
        _account_filter(Payment.account_id, account_ids),
        Payment.is_active.is_(True),
        Payment.status.in_(
            [
                PaymentStatus.succeeded,
                PaymentStatus.partially_refunded,
                PaymentStatus.refunded,
            ]
        ),
    )
    if currency is not None:
        payment_stmt = payment_stmt.where(Payment.currency == currency)
    for payment_row in db.execute(payment_stmt).all():
        if str(payment_row.account_id) in mirror_accounts and _before(
            payment_row.created_at, PAYMENT_ACTIVITY_AT
        ):
            continue
        net = _money(payment_row.amount) - _money(payment_row.refunded_amount)
        if net > ZERO:
            _add_position(
                positions,
                payment_row.account_id,
                payment_row.currency,
                net,
                credit=True,
            )

    allocation_stmt = (
        select(
            Invoice.account_id,
            Payment.currency,
            Invoice.currency.label("invoice_currency"),
            PaymentAllocation.amount,
            Payment.created_at,
        )
        .join(Invoice, Invoice.id == PaymentAllocation.invoice_id)
        .join(Payment, Payment.id == PaymentAllocation.payment_id)
        .where(
            _account_filter(Invoice.account_id, account_ids),
            PaymentAllocation.is_active.is_(True),
            Payment.is_active.is_(True),
            Payment.status.in_(
                [
                    PaymentStatus.succeeded,
                    PaymentStatus.partially_refunded,
                    PaymentStatus.refunded,
                ]
            ),
            or_(Payment.account_id.is_(None), Payment.account_id != Invoice.account_id),
        )
    )
    if currency is not None:
        allocation_stmt = allocation_stmt.where(Payment.currency == currency)
    for allocation_row in db.execute(allocation_stmt).all():
        if str(allocation_row.account_id) in mirror_accounts and _before(
            allocation_row.created_at, PAYMENT_ACTIVITY_AT
        ):
            continue
        _add_position(
            positions,
            allocation_row.account_id,
            allocation_row.currency or allocation_row.invoice_currency,
            allocation_row.amount,
            credit=True,
        )

    invoice_stmt = select(
        Invoice.account_id,
        Invoice.currency,
        Invoice.total,
        Invoice.created_at,
    ).where(
        _account_filter(Invoice.account_id, account_ids),
        Invoice.is_active.is_(True),
        Invoice.status.in_(
            [
                InvoiceStatus.issued,
                InvoiceStatus.partially_paid,
                InvoiceStatus.overdue,
                InvoiceStatus.paid,
            ]
        ),
        Invoice.is_proforma.is_(False),
        collectible_ar_invoice_filter(),
    )
    if currency is not None:
        invoice_stmt = invoice_stmt.where(Invoice.currency == currency)
    for invoice_row in db.execute(invoice_stmt).all():
        if str(invoice_row.account_id) in mirror_accounts and _before(
            invoice_row.created_at, SERVICE_ACTIVITY_AT
        ):
            continue
        _add_position(
            positions,
            invoice_row.account_id,
            invoice_row.currency,
            invoice_row.total,
            credit=False,
        )

    credit_note_stmt = select(
        CreditNote.account_id,
        CreditNote.currency,
        CreditNote.total,
        CreditNote.created_at,
    ).where(
        _account_filter(CreditNote.account_id, account_ids),
        CreditNote.is_active.is_(True),
        CreditNote.status.in_(
            [
                CreditNoteStatus.issued,
                CreditNoteStatus.partially_applied,
                CreditNoteStatus.applied,
            ]
        ),
    )
    if currency is not None:
        credit_note_stmt = credit_note_stmt.where(CreditNote.currency == currency)
    for credit_note_row in db.execute(credit_note_stmt).all():
        if str(credit_note_row.account_id) in mirror_accounts and _before(
            credit_note_row.created_at, SERVICE_ACTIVITY_AT
        ):
            continue
        _add_position(
            positions,
            credit_note_row.account_id,
            credit_note_row.currency,
            credit_note_row.total,
            credit=True,
        )

    ledger_stmt = select(
        LedgerEntry.account_id,
        LedgerEntry.currency,
        LedgerEntry.entry_type,
        LedgerEntry.amount,
        LedgerEntry.memo,
        LedgerEntry.effective_date,
        LedgerEntry.created_at,
    ).where(
        _account_filter(LedgerEntry.account_id, account_ids),
        LedgerEntry.invoice_id.is_(None),
        LedgerEntry.is_active.is_(True),
        or_(
            LedgerEntry.source.in_(
                [LedgerSource.adjustment, LedgerSource.refund, LedgerSource.other]
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
        ),
    )
    if currency is not None:
        ledger_stmt = ledger_stmt.where(LedgerEntry.currency == currency)
    for ledger_row in db.execute(ledger_stmt).all():
        memo = str(ledger_row.memo or "")
        if memo in INTERNAL_MEMO_EXACT or memo.startswith(INTERNAL_MEMO_PREFIXES):
            continue
        occurred_at = _aware(ledger_row.effective_date or ledger_row.created_at)
        if (
            str(ledger_row.account_id) in mirror_accounts
            and occurred_at is not None
            and occurred_at <= LEGACY_LEDGER_CUTOVER
        ):
            continue
        _add_position(
            positions,
            ledger_row.account_id,
            ledger_row.currency,
            ledger_row.amount,
            credit=ledger_row.entry_type == LedgerEntryType.credit,
        )

    return positions


def _batch_ledger_credit(
    db: Session, account_ids: Sequence[Any] | None, *, currency: str = "NGN"
) -> dict[str, Decimal]:
    # None means every account (one whole-table aggregate); only an explicitly
    # EMPTY set means there is nothing to do.
    if account_ids is not None and not account_ids:
        return {}
    rows = db.execute(
        select(
            LedgerEntry.account_id,
            LedgerEntry.entry_type,
            func.sum(LedgerEntry.amount).label("amount"),
        )
        .where(
            _account_filter(LedgerEntry.account_id, account_ids),
            LedgerEntry.invoice_id.is_(None),
            LedgerEntry.is_active.is_(True),
            LedgerEntry.currency == currency,
        )
        .group_by(LedgerEntry.account_id, LedgerEntry.entry_type)
    ).all()
    totals: dict[str, Decimal] = {}
    for row in rows:
        key = str(row.account_id)
        signed = (
            _money(row.amount)
            if row.entry_type == LedgerEntryType.credit
            else -_money(row.amount)
        )
        totals[key] = totals.get(key, ZERO) + signed
    return totals


# --------------------------------------------------------------------------
# D1 / D2 — ledger reversal integrity (F1)
# --------------------------------------------------------------------------


def d1_double_swings(db: Session) -> Finding:
    """Reversal entry posted AND the original deactivated.

    ``LedgerEntries.reverse`` (billing/ledger.py:112) does both. Every balance
    reader filters ``is_active``, so the original leaving the sum and the
    reversal entering it move the balance by 2x the amount.

    Only unallocated rows (``invoice_id IS NULL``) reach
    ``get_account_credit_balance`` and ``_ledger_event``, so the balance impact
    is real for those; invoice-allocated pairs are reported separately as
    ``balance_affecting=False``.
    """
    f = Finding("D1", "Ledger double-swing (reversal + deactivated original)", "F1")
    reversals = db.scalars(
        select(LedgerEntry).where(
            LedgerEntry.is_active.is_(True),
            LedgerEntry.memo.ilike("Reversal of ledger entry %"),
        )
    ).all()
    for rev in reversals:
        m = _REVERSAL_OF.search(rev.memo or "")
        if not m:
            continue
        original = db.get(LedgerEntry, m.group(1))
        if original is None or original.is_active:
            continue  # append-only and correct: nothing to fix
        balance_affecting = rev.invoice_id is None and original.invoice_id is None
        amt = _money(rev.amount)
        f.rows.append(
            {
                "account_id": str(rev.account_id),
                "original_id": str(original.id),
                "reversal_id": str(rev.id),
                "entry_type": getattr(original.entry_type, "value", ""),
                "amount": f"{amt:.2f}",
                "currency": rev.currency,
                "balance_affecting": balance_affecting,
                "overswing": f"{amt:.2f}" if balance_affecting else "0.00",
            }
        )
        if balance_affecting:
            f.amount += amt
    f.note = "amount = total over-swing on the credit balance (unallocated pairs only)"
    return f


def d2_unbacked_deactivated_credits(db: Session) -> Finding:
    """Deactivated customer CREDITS that no surviving document accounts for.

    The naive form of this check ("every deactivated ledger row is a silent
    balance move") is badly wrong and was rejected against real data: most
    deactivated pre-cutover rows duplicate the Payment/Invoice documents that
    ``customer_financial_ledger`` actually derives the balance from, so
    deactivating them removed a DOUBLE-COUNT rather than destroying money. On
    staging that benign case was 1,759 of 2,229 accounts.

    What is left over is the real question: a deactivated credit on an account
    with **no succeeded Payment document at all**. Nothing in the native schema
    then accounts for that money.

    IMPORTANT — this cannot be adjudicated where the Splynx mirror is absent.
    For a migrated account, pre-cutover money legitimately lives in
    ``splynx_billing_transactions``, and ``customer_financial_ledger`` serves it
    from there. If that table is empty (as on staging), a "missing" credit here
    may simply be a mirror that was never imported into this environment. The
    finding therefore reports mirror coverage alongside the money and refuses to
    call it a loss on its own.
    """
    f = Finding("D2", "Deactivated credit with no backing payment document", "F1")

    mirror_rows = 0
    try:
        from app.models.splynx_transaction import SplynxBillingTransaction

        mirror_rows = db.scalar(select(func.count(SplynxBillingTransaction.id))) or 0
    except Exception:  # noqa: BLE001 - mirror may not exist in this environment
        mirror_rows = 0

    payment_exists = (
        select(Payment.id)
        .where(
            Payment.account_id == LedgerEntry.account_id,
            Payment.is_active.is_(True),
            Payment.status == PaymentStatus.succeeded,
        )
        .exists()
    )
    rows = db.scalars(
        select(LedgerEntry).where(
            LedgerEntry.is_active.is_(False),
            LedgerEntry.invoice_id.is_(None),
            LedgerEntry.entry_type == LedgerEntryType.credit,
            ~payment_exists,
        )
    ).all()
    for e in rows:
        amt = _money(e.amount)
        f.rows.append(
            {
                "account_id": str(e.account_id),
                "entry_id": str(e.id),
                "amount": f"{amt:.2f}",
                "currency": e.currency,
                "effective_date": (
                    e.effective_date.isoformat() if e.effective_date else ""
                ),
            }
        )
        f.amount += amt

    if mirror_rows == 0:
        f.note = (
            "INCONCLUSIVE HERE: splynx_billing_transactions is EMPTY in this "
            "environment, so pre-cutover money has nowhere to live and this "
            "count cannot distinguish 'lost' from 'never imported'. Re-run "
            "read-only against an environment that has the mirror."
        )
    else:
        f.note = (
            f"mirror present ({mirror_rows:,} rows) — credits here are backed by "
            "neither a Payment document nor the legacy mirror"
        )
    return f


# --------------------------------------------------------------------------
# D3 / D10 — invoice integrity
# --------------------------------------------------------------------------


def d3_paid_with_balance(db: Session) -> Finding:
    """Invoice marked paid yet still carrying balance_due (F24 — fires on prod)."""
    f = Finding("D3", "Invoice paid but balance_due > 0", "F24")
    rows = db.scalars(
        select(Invoice).where(
            Invoice.is_active.is_(True),
            Invoice.status == InvoiceStatus.paid,
            Invoice.balance_due > 0,
        )
    ).all()
    for inv in rows:
        amt = _money(inv.balance_due)
        f.rows.append(
            {
                "account_id": str(inv.account_id),
                "invoice_id": str(inv.id),
                "invoice_number": inv.invoice_number or "",
                "total": f"{_money(inv.total):.2f}",
                "balance_due": f"{amt:.2f}",
                "currency": inv.currency,
            }
        )
        f.amount += amt
    return f


def d10_void_with_live_debits(db: Session) -> Finding:
    """Void invoices whose debit ledger entries were never reversed.

    ``Invoices.void`` reverses them; the repair services
    (billing_cleanup_remediation, billing_prepaid_overlap_repair) set status and
    balance_due directly and skip the reversal, so the customer keeps being
    charged in the ledger for an invoice that officially never existed.
    """
    f = Finding("D10", "Void invoice with active debit ledger entries", "F6/void")
    rows = db.execute(
        select(
            Invoice.id,
            Invoice.account_id,
            Invoice.invoice_number,
            func.sum(LedgerEntry.amount).label("debit_total"),
            func.count(LedgerEntry.id).label("debit_rows"),
        )
        .join(LedgerEntry, LedgerEntry.invoice_id == Invoice.id)
        .where(
            Invoice.status == InvoiceStatus.void,
            Invoice.is_active.is_(True),
            LedgerEntry.is_active.is_(True),
            LedgerEntry.entry_type == LedgerEntryType.debit,
        )
        .group_by(Invoice.id, Invoice.account_id, Invoice.invoice_number)
    ).all()
    for r in rows:
        amt = _money(r.debit_total)
        f.rows.append(
            {
                "account_id": str(r.account_id),
                "invoice_id": str(r.id),
                "invoice_number": r.invoice_number or "",
                "live_debit_rows": r.debit_rows,
                "live_debit_total": f"{amt:.2f}",
            }
        )
        f.amount += amt
    return f


# --------------------------------------------------------------------------
# D4 / D5 / D6 / D9 — payment integrity
# --------------------------------------------------------------------------


def d4_orphan_payments(db: Session) -> Finding:
    """Succeeded payments with neither an allocation nor a ledger entry (F19)."""
    f = Finding("D4", "Succeeded payment with no allocation and no ledger", "F19")
    alloc_exists = (
        select(PaymentAllocation.id)
        .where(
            PaymentAllocation.payment_id == Payment.id,
            PaymentAllocation.is_active.is_(True),
        )
        .exists()
    )
    ledger_exists = (
        select(LedgerEntry.id)
        .where(
            LedgerEntry.payment_id == Payment.id,
            LedgerEntry.is_active.is_(True),
        )
        .exists()
    )
    rows = db.scalars(
        select(Payment).where(
            Payment.is_active.is_(True),
            Payment.status == PaymentStatus.succeeded,
            ~alloc_exists,
            ~ledger_exists,
        )
    ).all()
    imported = 0
    imported_amount = ZERO
    for p in rows:
        amt = _money(p.amount)
        # A Splynx-imported payment legitimately has no local allocation or
        # ledger entry: it was mirrored, not posted. Counting those as orphans
        # inflates this class by ~3,100 rows / ~NGN 146M and is meaningless.
        # Only a NATIVE payment with no allocation and no ledger is the F19 bug.
        if getattr(p, "splynx_payment_id", None) is not None:
            imported += 1
            imported_amount += amt
            continue
        f.rows.append(
            {
                "account_id": str(p.account_id) if p.account_id else "",
                "payment_id": str(p.id),
                "amount": f"{amt:.2f}",
                "currency": p.currency,
                "external_id": p.external_id or "",
                "paid_at": p.paid_at.isoformat() if p.paid_at else "",
            }
        )
        f.amount += amt
    f.note = (
        "native orphans only — cash that settles nothing and is invisible to "
        f"credit. Excluded {imported:,} splynx-imported payments "
        f"(NGN {imported_amount:,.2f}), which are mirrored by design."
    )
    return f


def d5_misallocated_payments(db: Session) -> Finding:
    """Ledger credits pointing at a different invoice than the live allocation (F3).

    The admin payment-edit path (web_billing_payments.py:379) rewrites the
    allocation without touching the ledger, so the two disagree about which
    invoice the money settled.
    """
    f = Finding("D5", "Ledger invoice_id != live allocation invoice_id", "F3")
    rows = db.execute(
        select(
            Payment.id.label("payment_id"),
            Payment.account_id,
            Payment.amount,
            LedgerEntry.invoice_id.label("ledger_invoice"),
            PaymentAllocation.invoice_id.label("alloc_invoice"),
        )
        .join(
            LedgerEntry,
            and_(
                LedgerEntry.payment_id == Payment.id,
                LedgerEntry.is_active.is_(True),
                LedgerEntry.invoice_id.isnot(None),
            ),
        )
        .join(
            PaymentAllocation,
            and_(
                PaymentAllocation.payment_id == Payment.id,
                PaymentAllocation.is_active.is_(True),
            ),
        )
        .where(
            Payment.is_active.is_(True),
            LedgerEntry.invoice_id != PaymentAllocation.invoice_id,
        )
    ).all()
    for r in rows:
        amt = _money(r.amount)
        f.rows.append(
            {
                "account_id": str(r.account_id) if r.account_id else "",
                "payment_id": str(r.payment_id),
                "amount": f"{amt:.2f}",
                "ledger_invoice_id": str(r.ledger_invoice),
                "allocation_invoice_id": str(r.alloc_invoice),
            }
        )
        f.amount += amt
    f.note = "money double-counted: credited to one invoice, allocated to another"
    return f


def d6_succeeded_without_paid_at(db: Session) -> Finding:
    """Succeeded payments with NULL paid_at — blinds the enforcement health gate."""
    f = Finding("D6", "Succeeded payment with NULL paid_at", "F15/F19")
    rows = db.scalars(
        select(Payment).where(
            Payment.is_active.is_(True),
            Payment.status == PaymentStatus.succeeded,
            Payment.paid_at.is_(None),
        )
    ).all()
    for p in rows:
        amt = _money(p.amount)
        f.rows.append(
            {
                "account_id": str(p.account_id) if p.account_id else "",
                "payment_id": str(p.id),
                "amount": f"{amt:.2f}",
                "created_at": p.created_at.isoformat() if p.created_at else "",
                "external_id": p.external_id or "",
            }
        )
        f.amount += amt
    f.note = "recent-settlement volume is counted by paid_at; NULLs are invisible"
    return f


def d9_pending_money(db: Session) -> Finding:
    """Pending payments that already hold an allocation or a ledger credit (F18)."""
    f = Finding("D9", "Pending payment already holding allocation/ledger", "F18")
    alloc_exists = (
        select(PaymentAllocation.id)
        .where(
            PaymentAllocation.payment_id == Payment.id,
            PaymentAllocation.is_active.is_(True),
        )
        .exists()
    )
    ledger_exists = (
        select(LedgerEntry.id)
        .where(
            LedgerEntry.payment_id == Payment.id,
            LedgerEntry.is_active.is_(True),
        )
        .exists()
    )
    rows = db.scalars(
        select(Payment).where(
            Payment.is_active.is_(True),
            Payment.status == PaymentStatus.pending,
            or_(alloc_exists, ledger_exists),
        )
    ).all()
    for p in rows:
        amt = _money(p.amount)
        f.rows.append(
            {
                "account_id": str(p.account_id) if p.account_id else "",
                "payment_id": str(p.id),
                "amount": f"{amt:.2f}",
                "currency": p.currency,
            }
        )
        f.amount += amt
    f.note = (
        "credit moved but invoices never settle: _recalculate counts succeeded only"
    )
    return f


# --------------------------------------------------------------------------
# D7 / D8 — the two-balance split (F4)
# --------------------------------------------------------------------------


def d7_balance_definition_split(db: Session, limit: int, batch_size: int) -> Finding:
    """Accounts where the ledger credit and the document position disagree in SIGN.

    Settlement spends against ``get_account_credit_balance`` (ledger-derived,
    unallocated only). Enforcement and the portal read
    ``calculate_customer_balance`` (document-derived). They measure different
    things, so a raw delta is meaningless — but a *sign* disagreement is not:
    it means one subsystem sees a customer in credit while the other sees them
    in debt. That is the cohort that gets wrongly suspended or wrongly served.
    """
    f = Finding("D7", "Ledger credit and document position disagree in sign", "F4")
    account_ids = db.scalars(
        select(Subscriber.id)
        .where(Subscriber.is_active.is_(True))
        .order_by(Subscriber.id)
    ).all()
    if limit:
        account_ids = account_ids[:limit]

    # One whole-table aggregate per source, not a chunked IN(...) list per batch.
    # The chunked form re-scans ledger_entries once per chunk and blows the
    # statement timeout on the FIRST chunk (measured on staging). The aggregate
    # answers for every account in a single sequential scan. When --limit is set
    # we still scan the whole table and filter in Python: correctness is
    # identical, and the scan is what costs, not the row count returned.
    scan_ids = None if not limit else list(account_ids)
    ledger_by_account = _batch_ledger_credit(db, scan_ids)
    positions = _batch_customer_positions(db, scan_ids, currency="NGN")

    for batch in _chunks(account_ids, batch_size):
        for aid in batch:
            ledger_credit = ledger_by_account.get(str(aid), ZERO)
            doc_position = positions.get((str(aid), "NGN"), ZERO)
            disagree = (ledger_credit > ZERO and doc_position < ZERO) or (
                ledger_credit < ZERO and doc_position > ZERO
            )
            if not disagree:
                continue
            gap = abs(ledger_credit - doc_position)
            f.rows.append(
                {
                    "account_id": str(aid),
                    "ledger_credit": f"{ledger_credit:.2f}",
                    "document_position": f"{doc_position:.2f}",
                    "gap": f"{gap:.2f}",
                    "risk": (
                        "wrongly_suspended"
                        if ledger_credit > ZERO > doc_position
                        else "free_service"
                    ),
                }
            )
            f.amount += gap
    f.note = (
        "sign split only; amount = total absolute gap across disagreeing accounts; "
        f"balance derivation batched {batch_size} accounts at a time"
    )
    return f


def d8_unapplied_credit_notes(db: Session) -> Finding:
    """Issued credit notes with an unapplied remainder (F4 mechanism).

    An issued credit note raises the document balance immediately but posts no
    ledger entry until applied — so it is spendable to the portal and invisible
    to settlement.
    """
    f = Finding("D8", "Issued credit note not yet applied to the ledger", "F4")
    rows = db.scalars(
        select(CreditNote).where(
            CreditNote.is_active.is_(True),
            CreditNote.status.in_(
                [CreditNoteStatus.issued, CreditNoteStatus.partially_applied]
            ),
        )
    ).all()
    for cn in rows:
        remainder = _money(cn.total) - _money(getattr(cn, "applied_total", None))
        if remainder <= ZERO:
            continue
        f.rows.append(
            {
                "account_id": str(cn.account_id),
                "credit_note_id": str(cn.id),
                "status": getattr(cn.status, "value", ""),
                "total": f"{_money(cn.total):.2f}",
                "applied_total": f"{_money(getattr(cn, 'applied_total', None)):.2f}",
                "unapplied": f"{remainder:.2f}",
            }
        )
        f.amount += remainder
    f.note = "visible to the portal/enforcement balance, invisible to the credit ledger"
    return f


# --------------------------------------------------------------------------
# D11 — cutover opening-balance seed cohort
# --------------------------------------------------------------------------


def d11_opening_debits(db: Session) -> Finding:
    """Active opening-balance debits, split by the sign of subscribers.deposit."""
    f = Finding("D11", "Cutover opening-balance debits by deposit sign", "--")
    rows = db.execute(
        select(
            LedgerEntry.account_id,
            LedgerEntry.id,
            LedgerEntry.amount,
            LedgerEntry.memo,
            Subscriber.deposit,
        )
        .join(Subscriber, Subscriber.id == LedgerEntry.account_id)
        .where(
            LedgerEntry.is_active.is_(True),
            LedgerEntry.invoice_id.is_(None),
            LedgerEntry.entry_type == LedgerEntryType.debit,
            LedgerEntry.source == LedgerSource.adjustment,
            LedgerEntry.memo.ilike("%opening%"),
        )
    ).all()
    buckets: dict[str, Decimal] = {"positive": ZERO, "zero": ZERO, "negative": ZERO}
    for r in rows:
        dep = _money(r.deposit)
        bucket = "positive" if dep > ZERO else ("zero" if dep == ZERO else "negative")
        amt = _money(r.amount)
        buckets[bucket] += amt
        f.rows.append(
            {
                "account_id": str(r.account_id),
                "entry_id": str(r.id),
                "amount": f"{amt:.2f}",
                "deposit": f"{dep:.2f}",
                "deposit_sign": bucket,
                "verdict": {
                    "positive": "HOLD - deposit contradicts the seed",
                    "zero": "likely phantom - reversal nets to deposit",
                    "negative": "KEEP - genuine arrears",
                }[bucket],
            }
        )
        f.amount += amt
    f.note = " | ".join(f"{k}={v:.2f}" for k, v in buckets.items())
    return f


# --------------------------------------------------------------------------
# D12 — enforcement mismatch (F6/F7)
# --------------------------------------------------------------------------


def d12_enforcement_mismatch(db: Session, limit: int, batch_size: int) -> Finding:
    """Suspended-but-funded, and unfunded-but-active, prepaid accounts.

    Suspension keys on ``available < threshold``; the four ungated restore sites
    key on nothing at all. This reports both error directions.
    """
    from app.models.catalog import BillingMode, Subscription
    from app.models.enforcement_lock import EnforcementLock, EnforcementReason
    from app.services.collections.prepaid_balance_sweep import _RELEVANT_STATUSES
    from app.services.service_status import _prepaid_threshold

    f = Finding("D12", "Prepaid enforcement state vs funding", "F6/F7")
    # Money locks only. An admin/fraud/customer_hold lock is a deliberate
    # decision, not a billing mistake, and must not read as "wrongly suspended".
    locked_accounts = {
        str(a)
        for a in db.scalars(
            select(EnforcementLock.subscriber_id).where(
                EnforcementLock.is_active.is_(True),
                EnforcementLock.reason.in_(
                    [EnforcementReason.prepaid, EnforcementReason.overdue]
                ),
            )
        ).all()
    }
    # Restrict the detector to accounts that actually have a current prepaid
    # service. A non-prepaid account below the configured prepaid threshold is
    # not an enforcement mismatch.
    # DISTINCT the IDs, not the entities. Subscriber carries a `json` metadata
    # column and Postgres has no equality operator for json, so SELECT DISTINCT
    # subscribers.* fails outright ("could not identify an equality operator for
    # type json"). Select the distinct ids first, then load those rows.
    prepaid_ids = db.scalars(
        select(Subscriber.id)
        .join(Subscription, Subscription.subscriber_id == Subscriber.id)
        .where(
            Subscriber.is_active.is_(True),
            or_(
                Subscriber.billing_mode == BillingMode.prepaid,
                Subscription.billing_mode == BillingMode.prepaid,
            ),
            Subscription.status.in_(_RELEVANT_STATUSES),
        )
        .distinct()
        .order_by(Subscriber.id)
    ).all()
    if limit:
        prepaid_ids = prepaid_ids[:limit]

    subs = (
        db.scalars(
            select(Subscriber)
            .where(Subscriber.id.in_(prepaid_ids))
            .order_by(Subscriber.id)
        ).all()
        if prepaid_ids
        else []
    )

    # Same reason as D7: one whole-table aggregate, not a chunked IN(...) per
    # batch. currency=None because enforcement takes the MINIMUM across
    # currencies — a single-currency read would miss the cohort it suspends on.
    positions = _batch_customer_positions(
        db, None if not limit else list(prepaid_ids), currency=None
    )
    by_account: dict[str, list[Decimal]] = {}
    for (account_id, _currency), balance in positions.items():
        by_account.setdefault(account_id, []).append(balance)

    for batch in _chunks(subs, batch_size):
        for sub in batch:
            try:
                balances = by_account.get(str(sub.id), [])
                available = min(balances) if balances else ZERO
                threshold = _prepaid_threshold(db, sub) or ZERO
            except Exception:  # noqa: BLE001
                continue
            locked = str(sub.id) in locked_accounts
            funded = available >= threshold
            if locked and funded:
                verdict = "suspended_but_funded"
            elif not locked and not funded:
                verdict = "unfunded_but_active"
            else:
                continue
            f.rows.append(
                {
                    "account_id": str(sub.id),
                    "available": f"{available:.2f}",
                    "threshold": f"{threshold:.2f}",
                    "locked": locked,
                    "verdict": verdict,
                }
            )
            f.amount += abs(available - threshold)
    f.note = (
        "prepaid cohort only; suspended_but_funded = wrongly cut off; "
        "unfunded_but_active = free service; "
        f"balance derivation batched {batch_size} accounts at a time"
    )
    return f


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------


def _write_csv(out_dir: Path, finding: Finding) -> Path | None:
    if not finding.rows:
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{finding.code}_{finding.title[:40].replace(' ', '_')}.csv"
    fields = list(finding.rows[0].keys())
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(finding.rows)
    return path


def _configure_read_only_session(
    db: Session, *, statement_timeout_ms: int, allow_primary: bool
) -> None:
    """Apply database-side safety rails before the first audit query."""
    bind = db.get_bind()
    if bind is None or bind.dialect.name != "postgresql":
        return
    db.execute(text("SET TRANSACTION READ ONLY"))
    db.execute(text(f"SET LOCAL statement_timeout = {int(statement_timeout_ms)}"))
    is_replica = bool(db.scalar(text("SELECT pg_is_in_recovery()")))
    if not is_replica and not allow_primary:
        raise RuntimeError(
            "Refusing to run on a PostgreSQL primary. Use a read replica, or "
            "pass --allow-primary only after explicit production-host approval."
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", default="billing_alignment", help="directory for the per-class CSVs"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="cap accounts scanned in the per-account classes (D7, D12); 0 = all",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=250,
        help="accounts per batch for D7/D12 balance derivation (default: 250)",
    )
    parser.add_argument(
        "--statement-timeout-ms",
        type=int,
        default=10000,
        help="PostgreSQL statement timeout for each audit query (default: 10000)",
    )
    parser.add_argument(
        "--allow-primary",
        action="store_true",
        help="allow PostgreSQL primary execution after explicit host approval",
    )
    parser.add_argument(
        "--only", default="", help="comma-separated class codes, e.g. D1,D3,D12"
    )
    args = parser.parse_args()
    if args.batch_size <= 0:
        parser.error("--batch-size must be greater than zero")
    if args.statement_timeout_ms <= 0:
        parser.error("--statement-timeout-ms must be greater than zero")

    wanted = {c.strip().upper() for c in args.only.split(",") if c.strip()}
    out_dir = Path(args.out)

    checks: list[tuple[str, Callable[[Session], Finding]]] = [
        ("D1", d1_double_swings),
        ("D2", d2_unbacked_deactivated_credits),
        ("D3", d3_paid_with_balance),
        ("D4", d4_orphan_payments),
        ("D5", d5_misallocated_payments),
        ("D6", d6_succeeded_without_paid_at),
        (
            "D7",
            lambda db: d7_balance_definition_split(db, args.limit, args.batch_size),
        ),
        ("D8", d8_unapplied_credit_notes),
        ("D9", d9_pending_money),
        ("D10", d10_void_with_live_debits),
        ("D11", d11_opening_debits),
        (
            "D12",
            lambda db: d12_enforcement_mismatch(db, args.limit, args.batch_size),
        ),
    ]

    db = SessionLocal()
    findings: list[Finding] = []
    try:
        _configure_read_only_session(
            db,
            statement_timeout_ms=args.statement_timeout_ms,
            allow_primary=args.allow_primary,
        )
        for code, fn in checks:
            if wanted and code not in wanted:
                continue
            try:
                # A PostgreSQL statement timeout aborts the current savepoint.
                # Containing each class lets the runner roll that one class
                # back and continue without dropping the outer read-only
                # transaction or its safety settings.
                with db.begin_nested():
                    findings.append(fn(db))
            except Exception as exc:  # noqa: BLE001 - report, never abort the pass
                broken = Finding(code, f"FAILED: {exc}", "--")
                broken.error = traceback.format_exc(limit=3)
                findings.append(broken)
    finally:
        # Read-only by construction. Nothing above writes, and this guarantees it.
        db.rollback()
        db.close()

    width = 78
    print("=" * width)
    print("BILLING ALIGNMENT — READ-ONLY RECONCILIATION PASS")
    print("no writes, no commits; see docs/audits/BILLING_SOT_AUDIT_2026-07-12.md")
    print("=" * width)
    print(f"{'':<5}{'ROWS':>8}{'AMOUNT (NGN)':>18}  CLASS")
    print("-" * width)
    for f in findings:
        if f.error:
            print(f"{f.code:<5}{'ERR':>8}{'-':>18}  {f.title}")
            continue
        print(f"{f.code:<5}{f.count:>8}{f.amount:>18,.2f}  {f.title}  [{f.ref}]")
    print("-" * width)

    for f in findings:
        if f.error:
            print(f"\n{f.code} FAILED:\n{f.error}")
            continue
        if not f.rows:
            continue
        path = _write_csv(out_dir, f)
        print(f"\n{f.code}  {f.title}")
        if f.note:
            print(f"      note: {f.note}")
        if path:
            print(f"      csv:  {path}")

    total = sum((f.amount for f in findings if not f.error), ZERO)
    print(f"\ntotal money touched by at least one discrepancy class: NGN {total:,.2f}")
    print("(classes overlap — this is an upper bound, not a sum of distinct money)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
