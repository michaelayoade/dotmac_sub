"""Reclassify legacy prepaid advance invoices that became phantom AR.

Item 5 of docs/designs/PREPAID_INVOICE_DEPOSIT_ALIGNMENT.md. Before Item 1, the
runner issued prepaid renewal invoices due-on-issue; they aged to ``overdue``,
counted as AR, suppressed the wallet, and (with the flag on) opened dunning
cases — while the deposit was ALSO drawn down (double-count). Prod carried ~938
such prepaid ``issued``/``overdue`` invoices (₦25.9M).

This one-off brings each surviving prepaid AR invoice into the deposit-is-truth
model, per account (oldest invoice first):

* FUNDED — the account's payment-backed credit fully covers the invoice: settle
  it from that credit (``settle_single_invoice_from_credit`` — the same targeted,
  migrated-data-safe primitive Item 1 uses) → ``paid``. Revenue is recognised and
  the wallet drawn down once; the customer's *available* balance is unchanged
  (it already netted the open AR).
* UNFUNDED — the renewal was never funded: reclassify to ``draft`` (default;
  reversible, mirrors Item 1's draft-until-funded) or ``void`` (``--unfunded-action
  void``; terminal "should never have existed"). Either removes it from AR /
  overdue / dunning / balance by contract. This *raises* the customer's available
  balance by removing the phantom charge — the intended correction.

``partially_paid`` invoices carry real allocations; they are only ever settled if
now fully funded, never drafted/voided — otherwise reported for manual review.

Balance-neutral in the deposit-is-truth sense: funded periods are recognised,
unfunded periods are un-charged; no money is created or destroyed. Idempotent: a
processed row is stamped in ``metadata_`` and its status leaves the AR set, so a
re-run never touches it again.

Dry-run by default (side-effect free — it *simulates* per-account credit
consumption so the projection matches ``--apply``). Pass ``--apply`` to write.

Examples
--------
  python -m scripts.one_off.cleanup_prepaid_phantom_ar
  python -m scripts.one_off.cleanup_prepaid_phantom_ar --csv /tmp/plan.csv
  python -m scripts.one_off.cleanup_prepaid_phantom_ar --apply
  python -m scripts.one_off.cleanup_prepaid_phantom_ar --unfunded-action void --apply
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.billing import Invoice, InvoiceStatus
from app.models.catalog import BillingMode, Subscription, SubscriptionStatus
from app.models.subscriber import Subscriber
from app.services.billing._common import get_account_credit_balance
from app.services.billing.reconcile_unposted import (
    _allocatable_payments,
    settle_single_invoice_from_credit,
)
from app.services.common import round_money, to_decimal

CLEANUP_MARKER = "prepaid_phantom_ar_cleanup"
# Invoices that carry AR weight and are candidates for reclassification.
_AR_STATUSES = (
    InvoiceStatus.issued,
    InvoiceStatus.overdue,
    InvoiceStatus.partially_paid,
)
_RELEVANT_SUBSCRIPTION_STATUSES = (
    SubscriptionStatus.active,
    SubscriptionStatus.pending,
    SubscriptionStatus.suspended,
    SubscriptionStatus.blocked,
)


def _payment_backed_credit(db: Session, account_id: str, currency: str) -> Decimal:
    """Credit that real succeeded payments can back, for one currency — the same
    ``min(credit, payment_backed)`` ceiling ``settle_single_invoice_from_credit``
    spends within, so the dry-run projection matches ``--apply`` exactly."""
    credit = get_account_credit_balance(db, account_id, currency=currency)
    payment_backed = round_money(
        sum(
            (
                room
                for payment, room in _allocatable_payments(db, account_id)
                if (payment.currency or "NGN") == currency
            ),
            Decimal("0.00"),
        )
    )
    return min(max(credit, Decimal("0.00")), payment_backed)


def _prepaid_ar_invoices_by_account(
    db: Session,
) -> dict[str, list[Invoice]]:
    rows = (
        db.execute(
            select(Invoice)
            .join(Subscriber, Subscriber.id == Invoice.account_id)
            .where(
                Invoice.is_active.is_(True),
                Invoice.status.in_(_AR_STATUSES),
                Invoice.balance_due > Decimal("0.00"),
                (
                    (Subscriber.billing_mode == BillingMode.prepaid)
                    | exists()
                    .where(Subscription.subscriber_id == Invoice.account_id)
                    .where(Subscription.billing_mode == BillingMode.prepaid)
                    .where(Subscription.status.in_(_RELEVANT_SUBSCRIPTION_STATUSES))
                ),
            )
            .order_by(Invoice.account_id, Invoice.issued_at.asc().nulls_last())
        )
        .scalars()
        .all()
    )
    by_account: dict[str, list[Invoice]] = defaultdict(list)
    for inv in rows:
        if (inv.metadata_ or {}).get(CLEANUP_MARKER):
            continue  # already processed on a prior run
        by_account[str(inv.account_id)].append(inv)
    return by_account


def _mark(inv: Invoice, action: str, now: datetime) -> None:
    meta = dict(inv.metadata_ or {})
    meta[CLEANUP_MARKER] = {"action": action, "at": now.isoformat()}
    inv.metadata_ = meta


def run_cleanup(
    db: Session,
    *,
    apply: bool,
    unfunded_action: str,
    now: datetime | None = None,
) -> dict:
    """Reclassify prepaid AR invoices per account. Returns a summary + plan.

    Commits per account when ``apply`` (so one bad account can't abort the
    batch); writes nothing when ``apply`` is False. The dry-run projection
    simulates per-account credit consumption so it matches the ``apply`` result.
    """
    now = now or datetime.now(UTC)
    unfunded_status = (
        InvoiceStatus.draft if unfunded_action == "draft" else InvoiceStatus.void
    )
    n_funded = n_unfunded = n_partial = n_errors = 0
    amt_funded = Decimal("0.00")
    amt_unfunded = Decimal("0.00")
    plan: list[tuple[str, str, str, Decimal, str]] = []

    by_account = _prepaid_ar_invoices_by_account(db)
    for account_id, invoices in by_account.items():
        try:
            # Simulated per-account credit pool (dry-run) mirrors the live
            # consumption apply performs invoice-by-invoice.
            pools: dict[str, Decimal] = {}
            for inv in invoices:
                currency = inv.currency or "NGN"
                if currency not in pools:
                    pools[currency] = _payment_backed_credit(db, account_id, currency)
                balance = to_decimal(inv.balance_due)
                funded = pools[currency] >= balance and balance > Decimal("0.00")

                if funded:
                    pools[currency] = round_money(pools[currency] - balance)
                    action = "settle"
                    if apply:
                        settle_single_invoice_from_credit(db, inv, only_if_full=True)
                        db.flush()
                        if inv.status != InvoiceStatus.paid:
                            # Live credit fell short of the projection (a
                            # concurrent debit); leave it, flag for review.
                            action = "settle_incomplete"
                            n_partial += 1
                        else:
                            _mark(inv, "settled", now)
                            n_funded += 1
                            amt_funded = round_money(amt_funded + balance)
                    else:
                        n_funded += 1
                        amt_funded = round_money(amt_funded + balance)
                elif inv.status == InvoiceStatus.partially_paid:
                    # Real allocations exist and it isn't fully funded — never
                    # draft/void; surface for manual handling.
                    n_partial += 1
                    action = "review_partial"
                else:
                    n_unfunded += 1
                    amt_unfunded = round_money(amt_unfunded + balance)
                    action = unfunded_action
                    if apply:
                        inv.status = unfunded_status
                        inv.due_at = None
                        _mark(inv, unfunded_action, now)

                plan.append(
                    (account_id, str(inv.id), inv.status.value, balance, action)
                )
            if apply:
                db.commit()
        except Exception as exc:  # one bad account never aborts the batch
            db.rollback()
            n_errors += 1
            print(f"ERROR account {account_id}: {exc}")

    return {
        "accounts": len(by_account),
        "funded_settled": n_funded,
        "funded_amount": amt_funded,
        "unfunded_retired": n_unfunded,
        "unfunded_amount": amt_unfunded,
        "partial_review": n_partial,
        "errors": n_errors,
        "plan": plan,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes")
    parser.add_argument(
        "--unfunded-action",
        choices=("draft", "void"),
        default="draft",
        help="how to retire an unfunded prepaid AR invoice (default: draft)",
    )
    parser.add_argument(
        "--csv", type=Path, default=None, help="write the per-invoice plan to this CSV"
    )
    args = parser.parse_args()

    session = SessionLocal()
    try:
        result = run_cleanup(
            session, apply=args.apply, unfunded_action=args.unfunded_action
        )
        plan = result["plan"]
        mode = "APPLY" if args.apply else "DRY-RUN"
        print(
            f"{mode} [unfunded={args.unfunded_action}]: {result['accounts']} prepaid "
            f"accounts; settle-funded={result['funded_settled']} "
            f"(₦{result['funded_amount']}), retire-unfunded="
            f"{result['unfunded_retired']} (₦{result['unfunded_amount']}), "
            f"partial-review={result['partial_review']}, errors={result['errors']}"
        )

        if args.csv:
            with args.csv.open("w", newline="") as fh:
                writer = csv.writer(fh)
                writer.writerow(
                    ["account_id", "invoice_id", "status", "balance_due", "action"]
                )
                writer.writerows(plan)
            print(f"wrote {args.csv} ({len(plan)} rows)")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
