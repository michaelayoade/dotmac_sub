"""Reconcile cutover payments that posted as credit but never settled debt.

A cohort of ``succeeded`` gateway payments captured during the Splynx → local
cutover (from 2026-06-13) left the customer's *available balance* sitting as
unallocated credit while their invoices aged into suspension — the visible
symptom being "paid but still walled-garden". See
``app/services/billing/reconcile_unposted.py`` for the full background.

This CLI takes each affected account's available balance and applies it to the
account's open invoices (settling the debt exactly as if the money had posted at
invoice time), recomputes status via ``restore_account_services``, then — in
apply mode only — rebuilds RADIUS once and CoA-kicks the live sessions so the
walled-garden lifts immediately.

Dry-run by default; nothing is written without --apply.

Examples
--------
  # See the systemic cohort and what would settle (read-only):
  python -m scripts.one_off.reconcile_unposted_payments

  # Apply the fix to the whole cohort, refresh RADIUS, kick sessions:
  python -m scripts.one_off.reconcile_unposted_payments --apply

  # Also force RADIUS refresh + CoA onto specific reported logins that have no
  # remaining debt but a stuck walled-garden tag:
  python -m scripts.one_off.reconcile_unposted_payments --apply \
      --logins 100015097,100017641,100023828

  # Money only, leave RADIUS to the periodic safety-net + drift reconciler:
  python -m scripts.one_off.reconcile_unposted_payments --apply --no-radius --no-coa
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime

from app.db import SessionLocal
from app.models.catalog import Subscription
from app.services.billing.reconcile_unposted import reconcile_cohort

# Local-ledger cutover window. Payments from this date carry the posting gap.
DEFAULT_SINCE = "2026-06-13"


def _parse_since(value: str) -> datetime:
    dt = datetime.strptime(value, "%Y-%m-%d")
    return dt.replace(tzinfo=UTC)


def _resolve_logins(db, logins: list[str]) -> list[str]:
    """Map customer logins to their subscription ids for the targeted CoA pass."""
    if not logins:
        return []
    rows = (
        db.query(Subscription.id)
        .filter(Subscription.login.in_(logins))
        .all()
    )
    return [str(r[0]) for r in rows]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since",
        default=DEFAULT_SINCE,
        help=f"Only consider payments on/after this date (YYYY-MM-DD). Default {DEFAULT_SINCE}.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap the number of accounts processed (for a bounded first pass).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write changes. Without this flag the script is read-only.",
    )
    parser.add_argument(
        "--no-radius",
        action="store_true",
        help="Skip the synchronous RADIUS rebuild (apply mode only).",
    )
    parser.add_argument(
        "--no-coa",
        action="store_true",
        help="Skip CoA-kicking live sessions (apply mode only).",
    )
    parser.add_argument(
        "--notify",
        action="store_true",
        help=(
            "Send customer notifications for the settlements. OFF by default: "
            "this is bookkeeping catch-up over old funds across a mostly-churned "
            "cohort, so 'Payment received'/'Service resumed' mail would be wrong "
            "and a bulk burst would damage sender reputation."
        ),
    )
    parser.add_argument(
        "--logins",
        default="",
        help=(
            "Comma-separated customer logins to additionally force RADIUS refresh "
            "+ CoA onto, even if they have no remaining debt (the 'paid + active "
            "but still walled' cohort)."
        ),
    )
    args = parser.parse_args()

    since = _parse_since(args.since)
    dry_run = not args.apply
    logins = [s.strip() for s in args.logins.split(",") if s.strip()]

    db = SessionLocal()
    try:
        extra_subscription_ids = _resolve_logins(db, logins)
        if logins and not extra_subscription_ids:
            print(f"WARNING: none of the supplied logins matched a subscription: {logins}")

        summary = reconcile_cohort(
            db,
            since=since,
            limit=args.limit,
            dry_run=dry_run,
            refresh_radius=not args.no_radius,
            send_coa=not args.no_coa,
            extra_subscription_ids=extra_subscription_ids,
            notify=args.notify,
        )
    finally:
        db.close()

    mode = "DRY-RUN (no changes written)" if dry_run else "APPLY"
    print(f"\n=== Cutover payment reconcile — {mode} ===")
    print(f"since                  : {summary['since']}")
    print(f"candidate accounts     : {summary['candidates']}")
    print(f"accounts changed       : {summary['accounts_changed']}")
    print(f"total credit applied   : {summary['total_applied']}")
    print(f"invoices settled       : {summary['invoices_settled']}")
    print(f"subscriptions restored : {summary['subscriptions_restored']}")
    print(f"accounts w/ unbacked $ : {summary['unbacked_credit_accounts']}")
    print(f"errors                 : {summary['errors']}")
    if not dry_run:
        print(f"radius refreshed       : {summary['radius_refreshed']}")
        print(f"sessions kicked (CoA)  : {summary['sessions_kicked']}")

    # Per-account detail (dry-run shows the projected plan).
    results = summary.get("results", [])
    rows = [r for r in results if r.settle.applied > 0 or r.settle.unbacked_credit > 0 or r.error]
    if rows:
        print("\n--- per-account detail ---")
        for r in rows:
            s = r.settle
            note = f" ERROR: {r.error}" if r.error else ""
            unbacked = (
                f" (unbacked credit {s.unbacked_credit})" if s.unbacked_credit > 0 else ""
            )
            print(
                f"  {r.account_id}: credit={s.available_credit} "
                f"applied={s.applied} settled={len(s.invoices_settled)} "
                f"touched={len(s.invoices_touched)}{unbacked}{note}"
            )

    if dry_run:
        print("\nRe-run with --apply to write these changes.")


if __name__ == "__main__":
    main()
