#!/usr/bin/env python3
"""Classify the PAID/PARTIAL prepaid phantom invoices for finance disposition.

These are the invoices the cleanup script (cleanup_prepaid_phantom_invoices.py)
leaves as ``manual_review``: prepaid accounts that actually paid (or part-paid) a
phantom invoice. Voiding them would strand the payment, so each needs a finance
decision: keep the money as prepaid deposit/top-up credit, or refund.

READ-ONLY. This makes NO changes — it gathers evidence per invoice and assigns a
suggested class, writing a CSV for finance review.

Balance model note (see collections/_core.py:_resolve_prepaid_available_balance):
a settled Payment posts a ledger credit (source=payment); these phantom invoices
posted NO ledger debit, so a payment against one already sits as positive
balance. For migrated-but-unseeded accounts the imported ``deposit`` IS the
balance (the previous billing system already netted the payment), so the money is likewise already
reflected. "Already credited" means: do not also reallocate (that double-counts).

Suggested classes:
  * already_credited      — payment already reflected in balance/deposit AND
                            service is active. Void invoice (done) + annotate; no
                            reallocation (would duplicate credit).
  * reallocate_candidate  — service active but the payment is NOT yet reflected in
                            balance; represent it as prepaid deposit/top-up credit.
  * refund_candidate      — no active service basis to consume the credit; likely
                            mistaken/overpaid -> refund / manual finance action.
  * manual_finance_review — ambiguous (no allocation, no succeeded payment, mixed).

Usage (inside the app container)::

    python scripts/one_off/classify_paid_prepaid_phantoms.py --out /tmp
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.db import SessionLocal  # noqa: E402
from app.models.billing import (  # noqa: E402
    Invoice,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentAllocation,
    PaymentStatus,
)
from app.models.catalog import (  # noqa: E402
    BillingMode,
    Subscription,
    SubscriptionStatus,
)
from app.models.subscriber import Subscriber  # noqa: E402
from app.services.collections._core import (
    _resolve_prepaid_available_balance,  # noqa: E402
)

PAID_STATUSES = {InvoiceStatus.paid, InvoiceStatus.partially_paid}
ACTIVE_SUB = {SubscriptionStatus.active}
try:
    ACTIVE_SUB.add(SubscriptionStatus.activated)  # name varies across versions
except AttributeError:  # pragma: no cover
    pass


def manual_review_invoices(db) -> list[Invoice]:
    """Paid/partial prepaid phantom invoices (same targeting as the cleanup, but
    only the paid classes — these are never auto-voided)."""
    rows = (
        db.query(Invoice)
        .join(Subscriber, Subscriber.id == Invoice.account_id)
        .filter(Subscriber.billing_mode == BillingMode.prepaid)
        .filter(Invoice.splynx_invoice_id.is_(None))
        .filter(Invoice.added_by_id.is_(None))
        .filter(Invoice.billing_period_start.isnot(None))
        .filter(Invoice.invoice_number.isnot(None))
        .filter(Invoice.status.in_(PAID_STATUSES))
        .filter(Invoice.is_active.is_(True))
        .all()
    )
    out = []
    for inv in rows:
        meta = inv.metadata_ or {}
        if meta.get("credit_exception") or meta.get("void_reason"):
            continue
        out.append(inv)
    return out


def evidence(db, inv: Invoice) -> dict:
    sub_acct = db.get(Subscriber, inv.account_id)
    allocs = (
        db.query(PaymentAllocation)
        .filter(PaymentAllocation.invoice_id == inv.id)
        .filter(PaymentAllocation.is_active.is_(True))
        .all()
    )
    payments = []
    any_succeeded = False
    all_have_credit = bool(allocs)
    for a in allocs:
        p = db.get(Payment, a.payment_id)
        succeeded = bool(p and p.status == PaymentStatus.succeeded)
        any_succeeded = any_succeeded or succeeded
        has_credit = (
            db.query(LedgerEntry.id)
            .filter(LedgerEntry.payment_id == a.payment_id)
            .filter(LedgerEntry.entry_type == LedgerEntryType.credit)
            .filter(LedgerEntry.source == LedgerSource.payment)
            .filter(LedgerEntry.is_active.is_(True))
            .first()
            is not None
        )
        all_have_credit = all_have_credit and has_credit
        payments.append(
            {
                "payment_id": str(a.payment_id),
                "alloc_amount": str(a.amount),
                "payment_amount": str(p.amount) if p else "",
                "payment_status": getattr(p.status, "value", "") if p else "missing",
                "has_ledger_credit": has_credit,
            }
        )

    service_active = (
        db.query(Subscription.id)
        .filter(Subscription.subscriber_id == inv.account_id)
        .filter(Subscription.status.in_(list(ACTIVE_SUB)))
        .first()
        is not None
    )
    splynx_linked = bool(sub_acct and sub_acct.splynx_customer_id is not None)
    deposit = getattr(sub_acct, "deposit", None) if sub_acct else None
    try:
        avail = _resolve_prepaid_available_balance(db, str(inv.account_id))
    except Exception:  # noqa: BLE001
        avail = None
    meta = inv.metadata_ or {}
    return {
        "invoice_number": inv.invoice_number,
        "account_id": str(inv.account_id),
        "status": getattr(inv.status, "value", inv.status),
        "orig_total": meta.get("original_total", str(inv.total)),
        "orig_balance_due": meta.get("original_balance_due", str(inv.balance_due)),
        "num_allocations": len(allocs),
        "any_payment_succeeded": any_succeeded,
        "all_allocated_have_credit": all_have_credit,
        "splynx_linked": splynx_linked,
        "deposit": str(deposit) if deposit is not None else "",
        "available_balance": str(avail) if avail is not None else "",
        "service_active": service_active,
        "payments": payments,
    }


def classify_one(ev: dict) -> str:
    if ev["num_allocations"] == 0 or not ev["any_payment_succeeded"]:
        return "manual_finance_review"
    # money already reflected in balance: local ledger credit exists for every
    # allocated payment, OR the (unseeded) imported deposit already nets it.
    credited = ev["all_allocated_have_credit"] or (
        ev["splynx_linked"] and bool(ev["deposit"])
    )
    if not ev["service_active"]:
        return "refund_candidate"
    return "already_credited" if credited else "reallocate_candidate"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=".", help="directory for the CSV artifact")
    args = ap.parse_args()
    out = Path(args.out)

    db = SessionLocal()
    try:
        invs = manual_review_invoices(db)
        rows, counts = [], {}
        for inv in invs:
            ev = evidence(db, inv)
            cls = classify_one(ev)
            counts[cls] = counts.get(cls, 0) + 1
            rows.append(
                {
                    "suggested_class": cls,
                    "invoice_number": ev["invoice_number"],
                    "account_id": ev["account_id"],
                    "status": ev["status"],
                    "orig_total": ev["orig_total"],
                    "orig_balance_due": ev["orig_balance_due"],
                    "num_allocations": ev["num_allocations"],
                    "any_payment_succeeded": ev["any_payment_succeeded"],
                    "all_allocated_have_credit": ev["all_allocated_have_credit"],
                    "splynx_linked": ev["splynx_linked"],
                    "deposit": ev["deposit"],
                    "available_balance": ev["available_balance"],
                    "service_active": ev["service_active"],
                    "payments": ev["payments"],
                }
            )
        path = out / "prepaid_phantom_paid_classification.csv"
        with path.open("w", newline="") as fh:
            cols = list(rows[0].keys()) if rows else ["suggested_class"]
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow(r)
        print(f"{len(invs)} paid/partial prepaid phantom invoice(s) -> {path}")
        for cls in (
            "already_credited",
            "reallocate_candidate",
            "refund_candidate",
            "manual_finance_review",
        ):
            print(f"  {cls:22}: {counts.get(cls, 0)}")
        print(
            "\nREAD-ONLY: no changes made. Finance reviews the CSV before any action."
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
