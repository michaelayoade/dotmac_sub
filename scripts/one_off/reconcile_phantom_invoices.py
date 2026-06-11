"""Reconcile phantom (Splynx duplicate) invoices.

The local invoice runner re-billed pre-cutover periods already settled in
Splynx (the authoritative system), creating open local invoices with
``splynx_invoice_id IS NULL`` and billing periods before the migration cutover.
These inflate customer balances and have driven false overdue/suspension events
(see the 2026-05/06 phantom-invoice incident).

Candidate = an OPEN invoice (status issued/overdue/partially_paid,
balance_due > 0) with ``splynx_invoice_id IS NULL`` whose
``billing_period_start`` is before ``--cutover`` (default 2026-01-01), for a
subscriber that is Splynx-origin (has a subscription with a splynx_service_id).

Three phases, applied in order; each is independently safe and idempotent:

  flag    set metadata.reconciliation_hold = true  → immediately stops the
          overdue/dunning sweep from touching them (reversible).
  void    void the flagged invoices (status=void, balance_due=0, reversing
          ledger entries) via the audited invoice-void path.
  restore un-suspend subscribers whose remaining real (non-phantom) open
          balance is now 0.

Dry-run by default; nothing is written without --apply. Run flag first, eyeball
the totals, then re-run with --phase void and --phase restore.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import and_, exists, select

from app.db import SessionLocal
from app.models.billing import Invoice, InvoiceStatus, PaymentAllocation
from app.models.catalog import Subscription, SubscriptionStatus
from app.services import billing as billing_service
from app.services.account_lifecycle import restore_subscription

_OPEN_STATUSES = (
    InvoiceStatus.issued,
    InvoiceStatus.overdue,
    InvoiceStatus.partially_paid,
)


def _candidate_query(db, cutover: datetime):
    """Open NULL-Splynx pre-cutover invoices for Splynx-origin subscribers."""
    splynx_origin = exists(
        select(Subscription.id).where(
            and_(
                Subscription.subscriber_id == Invoice.account_id,
                Subscription.splynx_service_id.is_not(None),
            )
        )
    )
    return (
        db.query(Invoice)
        .filter(Invoice.is_active.is_(True))
        .filter(Invoice.splynx_invoice_id.is_(None))
        .filter(Invoice.status.in_(_OPEN_STATUSES))
        .filter(Invoice.balance_due > Decimal("0.00"))
        .filter(Invoice.billing_period_start.isnot(None))
        .filter(Invoice.billing_period_start < cutover)
        .filter(splynx_origin)
        .order_by(Invoice.account_id, Invoice.billing_period_start)
    )


def _phase_flag(db, candidates, apply: bool, stats: Counter):
    for inv in candidates:
        meta = dict(inv.metadata_ or {})
        if meta.get("reconciliation_hold"):
            stats["already_flagged"] += 1
            continue
        stats["to_flag"] += 1
        stats["to_flag_amount"] += int(inv.balance_due)
        if apply:
            meta["reconciliation_hold"] = True
            meta["reconciliation_flagged_at"] = datetime.now(UTC).isoformat()
            inv.metadata_ = meta
    if apply:
        db.commit()


def _phase_void(db, candidates, apply: bool, stats: Counter):
    for inv in candidates:
        meta = dict(inv.metadata_ or {})
        # Only void what was flagged (and reviewed) in the flag phase.
        if not meta.get("reconciliation_hold"):
            stats["skipped_unflagged"] += 1
            continue
        # Safety: never void an invoice that has a payment allocated to it —
        # voiding would strand a real payment. Leave it for manual review.
        has_payment = (
            db.query(PaymentAllocation)
            .filter(PaymentAllocation.invoice_id == inv.id)
            .first()
            is not None
        )
        if has_payment:
            stats["skipped_has_payment"] += 1
            continue
        stats["to_void"] += 1
        stats["to_void_amount"] += int(inv.balance_due)
        if apply:
            billing_service.invoices.void(
                db,
                str(inv.id),
                memo="Voided: Splynx duplicate (pre-cutover phantom invoice)",
            )
    # void() commits per invoice.


def _phase_restore(db, cutover: datetime, apply: bool, stats: Counter):
    """Restore subscribers suspended while carrying only phantom debt."""
    suspended = (
        db.query(Subscription)
        .filter(
            Subscription.status.in_(
                [
                    SubscriptionStatus.blocked,
                    SubscriptionStatus.suspended,
                    SubscriptionStatus.stopped,
                ]
            )
        )
        .filter(Subscription.splynx_service_id.is_not(None))
        .all()
    )
    for sub in suspended:
        real_open = (
            db.query(Invoice)
            .filter(Invoice.account_id == sub.subscriber_id)
            .filter(Invoice.is_active.is_(True))
            .filter(Invoice.status.in_(_OPEN_STATUSES))
            .filter(Invoice.balance_due > Decimal("0.00"))
            .filter(
                (Invoice.splynx_invoice_id.is_not(None))
                | (Invoice.billing_period_start >= cutover)
                | (Invoice.billing_period_start.is_(None))
            )
            .count()
        )
        if real_open > 0:
            stats["restore_skipped_real_debt"] += 1
            continue
        stats["to_restore"] += 1
        if apply:
            restore_subscription(
                db,
                str(sub.id),
                trigger="admin",
                resolved_by="reconcile_phantom_invoices",
            )
    if apply:
        db.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write changes.")
    parser.add_argument(
        "--phase",
        choices=["flag", "void", "restore"],
        default="flag",
        help="Which remediation phase to run (default: flag).",
    )
    parser.add_argument(
        "--cutover",
        type=lambda s: datetime.fromisoformat(s).replace(tzinfo=UTC),
        default=datetime(2026, 1, 1, tzinfo=UTC),
        help="Period-start cutover; invoices before it are phantom. Default 2026-01-01.",
    )
    args = parser.parse_args()

    db = SessionLocal()
    stats: Counter = Counter()
    try:
        if args.phase == "restore":
            _phase_restore(db, args.cutover, args.apply, stats)
        else:
            candidates = _candidate_query(db, args.cutover).all()
            stats["candidates"] = len(candidates)
            stats["candidates_subs"] = len({str(i.account_id) for i in candidates})
            if args.phase == "flag":
                _phase_flag(db, candidates, args.apply, stats)
            else:
                _phase_void(db, candidates, args.apply, stats)
    finally:
        db.close()

    mode = "APPLIED" if args.apply else "DRY RUN"
    print(f"\n=== reconcile_phantom_invoices [{args.phase}] ({mode}) ===")
    for k, v in sorted(stats.items()):
        print(f"  {k}: {v}")
    if not args.apply:
        print("\n(dry run — re-run with --apply to write)")


if __name__ == "__main__":
    main()
