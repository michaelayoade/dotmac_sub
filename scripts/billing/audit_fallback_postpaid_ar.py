#!/usr/bin/env python
"""Adjudicate the Splynx-deposit-fallback postpaid accounts (READ-ONLY).

Two reports for the billing data-hardening slice (see
docs/POST_CUTOVER_HARDENING.md). Writes nothing to the DB.

Report A — AR trustworthiness of the fallback accounts. For each account still
on the ``account.deposit`` fallback, does the local AR ledger explain the
balance, or is it a migration gap? Decision rule: local AR wins ONLY if
populated enough to explain the account.

Report B — un-wall impact. Compare the CURRENT signal (``deposit >= 0``) with
the PROPOSED signal (``not has_overdue_balance``) and list only the accounts
whose walling decision would flip.

CSVs are written to the repo root (untracked analysis artifacts).

Usage:
    docker compose exec -T -e PYTHONPATH=/app app \
        python scripts/billing/audit_fallback_postpaid_ar.py
"""

from __future__ import annotations

import csv
import sys
from collections import Counter
from decimal import Decimal

from sqlalchemy import func

from app.db import SessionLocal
from app.models.billing import (
    Invoice,
    InvoiceStatus,
    LedgerEntry,
    Payment,
    PaymentStatus,
)
from app.models.subscriber import Subscriber
from app.services.billing._common import get_account_credit_balance
from app.services.collections._core import has_overdue_balance
from app.services.prepaid_billing import PREPAID_OPENING_BALANCE_MEMO

_TOL = Decimal("0.01")
_OPEN_STATUSES = [
    InvoiceStatus.issued,
    InvoiceStatus.partially_paid,
    InvoiceStatus.overdue,
]
_PAID_STATUSES = [PaymentStatus.succeeded, PaymentStatus.partially_refunded]

AR_CSV = "/app/post_cutover_fallback_postpaid_ar.csv"
UNWALL_CSV = "/app/post_cutover_fallback_unwall_diff.csv"

COLUMNS = [
    "subscriber_id",
    "splynx_customer_id",
    "status",
    "billing_mode",
    "deposit",
    "ledger_credit_balance",
    "open_invoice_balance",
    "proposed_available_balance",
    "delta_deposit_vs_proposed",
    "has_overdue_balance",
    "active_invoice_count",
    "payment_count",
    "ledger_entry_count",
    "last_invoice_at",
    "last_payment_at",
    "classification",
]


def _classify(row: dict) -> str:
    status = row["status"]
    deposit = row["deposit"]
    delta = row["delta_deposit_vs_proposed"]
    inv = row["active_invoice_count"]
    pays = row["payment_count"]

    if status in {"canceled", "disabled"}:
        return "canceled_or_disabled_review"
    if abs(delta) <= _TOL:
        return "ledger_reconciles"
    if deposit > _TOL:
        return "deposit_credit_on_postpaid"
    if inv == 0 and deposit < -_TOL:
        return "ledger_missing_invoices"
    if pays == 0 and delta > _TOL:
        # ledger shows MORE owed than the deposit and no payments migrated
        return "ledger_missing_payments"
    if inv > 0 and pays > 0:
        return "ledger_has_ar_but_differs"
    return "manual_finance_review"


def main() -> int:
    db = SessionLocal()
    try:
        seeded_ids = {
            r[0]
            for r in db.query(LedgerEntry.account_id)
            .filter(LedgerEntry.memo == PREPAID_OPENING_BALANCE_MEMO)
            .filter(LedgerEntry.is_active.is_(True))
            .all()
        }
        accounts = [
            a
            for a in db.query(Subscriber)
            .filter(Subscriber.splynx_customer_id.isnot(None))
            .filter(Subscriber.deposit.isnot(None))
            .all()
            if a.id not in seeded_ids
        ]

        rows: list[dict] = []
        for a in accounts:
            aid = str(a.id)
            deposit = Decimal(str(a.deposit))
            credit = Decimal(str(get_account_credit_balance(db, aid)))
            open_bal = Decimal(
                str(
                    db.query(func.coalesce(func.sum(Invoice.balance_due), 0))
                    .filter(Invoice.account_id == a.id)
                    .filter(Invoice.is_active.is_(True))
                    .filter(Invoice.status.in_(_OPEN_STATUSES))
                    .scalar()
                    or 0
                )
            )
            proposed = credit - open_bal
            inv_count = (
                db.query(func.count(Invoice.id))
                .filter(Invoice.account_id == a.id)
                .filter(Invoice.is_active.is_(True))
                .scalar()
            )
            pay_count = (
                db.query(func.count(Payment.id))
                .filter(Payment.account_id == a.id)
                .filter(Payment.is_active.is_(True))
                .filter(Payment.status.in_(_PAID_STATUSES))
                .scalar()
            )
            ledg_count = (
                db.query(func.count(LedgerEntry.id))
                .filter(LedgerEntry.account_id == a.id)
                .filter(LedgerEntry.is_active.is_(True))
                .scalar()
            )
            last_inv = (
                db.query(func.max(func.coalesce(Invoice.issued_at, Invoice.created_at)))
                .filter(Invoice.account_id == a.id)
                .filter(Invoice.is_active.is_(True))
                .scalar()
            )
            last_pay = (
                db.query(func.max(func.coalesce(Payment.paid_at, Payment.created_at)))
                .filter(Payment.account_id == a.id)
                .filter(Payment.is_active.is_(True))
                .filter(Payment.status.in_(_PAID_STATUSES))
                .scalar()
            )
            row = {
                "subscriber_id": aid,
                "splynx_customer_id": a.splynx_customer_id,
                "status": getattr(a.status, "value", str(a.status)),
                "billing_mode": getattr(a.billing_mode, "value", str(a.billing_mode)),
                "deposit": deposit,
                "ledger_credit_balance": credit,
                "open_invoice_balance": open_bal,
                "proposed_available_balance": proposed,
                "delta_deposit_vs_proposed": deposit - proposed,
                "has_overdue_balance": has_overdue_balance(db, aid),
                "active_invoice_count": inv_count,
                "payment_count": pay_count,
                "ledger_entry_count": ledg_count,
                "last_invoice_at": last_inv.isoformat() if last_inv else "",
                "last_payment_at": last_pay.isoformat() if last_pay else "",
            }
            row["classification"] = _classify(row)
            rows.append(row)

        with open(AR_CSV, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=COLUMNS)
            w.writeheader()
            for r in rows:
                w.writerow({k: r[k] for k in COLUMNS})

        # Report B — un-wall signal diff.
        diffs = []
        for r in rows:
            current = r["deposit"] >= 0
            proposed_signal = not r["has_overdue_balance"]
            if current != proposed_signal:
                diffs.append(
                    {
                        "subscriber_id": r["subscriber_id"],
                        "status": r["status"],
                        "deposit": r["deposit"],
                        "has_overdue_balance": r["has_overdue_balance"],
                        "current_unwall_signal": current,
                        "proposed_unwall_signal": proposed_signal,
                        "direction": (
                            "would_WALL (currently unwalled)"
                            if current and not proposed_signal
                            else "would_UNWALL (currently walled)"
                        ),
                        "classification": r["classification"],
                    }
                )
        with open(UNWALL_CSV, "w", newline="") as fh:
            cols = [
                "subscriber_id",
                "status",
                "deposit",
                "has_overdue_balance",
                "current_unwall_signal",
                "proposed_unwall_signal",
                "direction",
                "classification",
            ]
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            for d in diffs:
                w.writerow(d)

        # Summary.
        print(f"fallback accounts analysed : {len(rows)}")
        print(f"AR report                  : {AR_CSV}")
        print(f"un-wall diff report        : {UNWALL_CSV}")
        print("\n-- classification counts --")
        cls = Counter(r["classification"] for r in rows)
        for name, n in cls.most_common():
            print(f"  {name:30s} {n:>4}")
        divergent = [r for r in rows if abs(r["delta_deposit_vs_proposed"]) > _TOL]
        print(f"\ndivergent (delta > tol)    : {len(divergent)}")
        print("\n-- un-wall impact --")
        print(f"  accounts whose signal flips: {len(diffs)}")
        dir_counts = Counter(d["direction"] for d in diffs)
        for name, n in dir_counts.most_common():
            print(f"    {name:34s} {n:>4}")
        active_flips = [d for d in diffs if d["status"] in {"active", "blocked"}]
        print(f"  ...of which active/blocked : {len(active_flips)}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
