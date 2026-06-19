#!/usr/bin/env python
"""Characterize the Splynx-deposit-fallback accounts (READ-ONLY, dry-run).

The billing data-hardening slice removes the ``account.deposit`` fallback in
``_resolve_prepaid_available_balance``. That is only safe once every
Splynx-linked account with a deposit has been seeded onto the local ledger.
This report classifies the still-unseeded ("on the fallback") accounts:

  - WHY each is unseeded (which skip condition applies). The seeder
    (scripts/billing/seed_prepaid_opening_balance.py) only processes
    ``billing_mode == prepaid``; the fallback fires for ANY splynx-linked
    account with a deposit, so non-prepaid accounts never get seeded.
  - WHAT seed would be written: the true-up delta
    ``deposit − (ledger_net − open_invoices)``, exactly as the seeder computes
    it (credit if > 0, debit if < 0, zero-marker if ≈ 0).

Totals are grouped by skip reason, billing_mode, and account status. Writes
nothing. See docs/POST_CUTOVER_HARDENING.md.

Usage (in the app container):
    docker compose exec -T -e PYTHONPATH=/app app \
        python scripts/billing/characterize_fallback_accounts.py
"""

from __future__ import annotations

import sys
from collections import defaultdict
from decimal import Decimal

from sqlalchemy import case, func

from app.db import SessionLocal
from app.models.billing import (
    Invoice,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
)
from app.models.subscriber import Subscriber
from app.services.prepaid_billing import PREPAID_OPENING_BALANCE_MEMO

_TOL = Decimal("0.01")


def _money(x: Decimal) -> str:
    return f"NGN {x:,.2f}"


def main() -> int:
    db = SessionLocal()
    try:
        # Fallback population: splynx-linked + has deposit + NO opening-balance
        # seed (any entry type, matching the resolver's "seeded" test).
        seeded_ids = {
            r[0]
            for r in db.query(LedgerEntry.account_id)
            .filter(LedgerEntry.memo == PREPAID_OPENING_BALANCE_MEMO)
            .filter(LedgerEntry.is_active.is_(True))
            .all()
        }
        accounts = (
            db.query(Subscriber)
            .filter(Subscriber.splynx_customer_id.isnot(None))
            .filter(Subscriber.deposit.isnot(None))
            .all()
        )
        fallback = [a for a in accounts if a.id not in seeded_ids]

        # Ledger net (unallocated, active) and open-invoice balance per account —
        # same model the seeder/resolver use.
        signed = func.sum(
            case(
                (LedgerEntry.entry_type == LedgerEntryType.credit, LedgerEntry.amount),
                else_=-LedgerEntry.amount,
            )
        )
        ledger_net = {
            aid: Decimal(str(net or 0))
            for aid, net in db.query(LedgerEntry.account_id, signed)
            .filter(LedgerEntry.invoice_id.is_(None))
            .filter(LedgerEntry.is_active.is_(True))
            .group_by(LedgerEntry.account_id)
            .all()
        }
        open_inv = {
            aid: Decimal(str(bal or 0))
            for aid, bal in db.query(Invoice.account_id, func.sum(Invoice.balance_due))
            .filter(Invoice.is_active.is_(True))
            .filter(
                Invoice.status.in_(
                    [
                        InvoiceStatus.issued,
                        InvoiceStatus.partially_paid,
                        InvoiceStatus.overdue,
                    ]
                )
            )
            .group_by(Invoice.account_id)
            .all()
        }

        # Per-account classification.
        by_reason: dict[str, list] = defaultdict(list)
        by_mode: dict[str, list] = defaultdict(list)
        by_status: dict[str, int] = defaultdict(int)
        seed_shape = {"credit": 0, "debit": 0, "marker": 0}
        delta_credit_total = Decimal("0")
        delta_debit_total = Decimal("0")
        deposit_total = Decimal("0")

        for a in fallback:
            mode = getattr(a.billing_mode, "value", str(a.billing_mode))
            status = getattr(a.status, "value", str(a.status))
            deposit = Decimal(str(a.deposit))
            existing = ledger_net.get(a.id, Decimal("0")) - open_inv.get(
                a.id, Decimal("0")
            )
            delta = deposit - existing

            reason = (
                "excluded_by_seeder_not_prepaid"
                if mode != "prepaid"
                else "prepaid_but_unseeded"
            )
            rec = (a.id, mode, status, deposit, delta)
            by_reason[reason].append(rec)
            by_mode[mode].append(rec)
            by_status[status] += 1
            deposit_total += deposit
            if delta > _TOL:
                seed_shape["credit"] += 1
                delta_credit_total += delta
            elif delta < -_TOL:
                seed_shape["debit"] += 1
                delta_debit_total += -delta
            else:
                seed_shape["marker"] += 1

        print("=== Splynx-deposit-fallback characterization (READ-ONLY) ===")
        print(f"accounts still on the fallback : {len(fallback)}")
        print(f"total deposit carried          : {_money(deposit_total)}")
        print()
        print("-- by skip reason --")
        for reason, recs in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
            dep = sum((r[3] for r in recs), Decimal("0"))
            print(f"  {reason:32s} {len(recs):>4}  deposit {_money(dep)}")
        print()
        print("-- by billing_mode --")
        for mode, recs in sorted(by_mode.items(), key=lambda kv: -len(kv[1])):
            dep = sum((r[3] for r in recs), Decimal("0"))
            print(f"  {str(mode):12s} {len(recs):>4}  deposit {_money(dep)}")
        print()
        print("-- by account status --")
        for status, n in sorted(by_status.items(), key=lambda kv: -kv[1]):
            print(f"  {str(status):12s} {n:>4}")
        print()
        print("-- proposed seed shape (delta = deposit − ledger_net) --")
        print(
            f"  credit (deposit > ledger) : {seed_shape['credit']:>4}  "
            f"total {_money(delta_credit_total)}"
        )
        print(
            f"  debit  (deposit < ledger) : {seed_shape['debit']:>4}  "
            f"total {_money(delta_debit_total)}"
        )
        print(f"  marker (already equal)    : {seed_shape['marker']:>4}")
        print(
            f"  net ledger impact         : "
            f"{_money(delta_credit_total - delta_debit_total)}"
        )
        print()
        print("-- sample (first 15) --")
        for aid, mode, status, deposit, delta in [
            (r[0], r[1], r[2], r[3], r[4]) for recs in by_reason.values() for r in recs
        ][:15]:
            print(
                f"  {str(aid)[:8]}  mode={mode:10s} status={status:10s} "
                f"deposit={_money(deposit):>16}  seed_delta={_money(delta):>16}"
            )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
