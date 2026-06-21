#!/usr/bin/env python
"""Reconcile stale ``overdue`` enforcement locks (dry-run first).

Clears ``overdue`` enforcement locks that are still active while the account owes
NO overdue debt (stale drift that keeps a paid-up service suspended), and
reactivates subs held by nothing else. A sub also held by another lock (e.g. a
wrongful ``prepaid`` lock — run restore_prepaid_suspended_postpaid.py too) has
its overdue lock cleared but stays suspended (reported). See
app/services/stale_overdue_lock_reconcile.py.

NO ledger / money writes.

Usage (inside the app container; PYTHONPATH=/app):

    # dry run — sizes the cohort + projected outcome, writes nothing (default)
    docker compose exec -T -e PYTHONPATH=/app app \
        python scripts/one_off/reconcile_stale_overdue_locks.py

    # apply, capped to a few first
    docker compose exec -T -e PYTHONPATH=/app app \
        python scripts/one_off/reconcile_stale_overdue_locks.py --apply --limit 10
"""

import argparse
import json
import sys
from dataclasses import asdict

from app.db import SessionLocal
from app.services.stale_overdue_lock_reconcile import reconcile


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Clear the stale locks + reactivate. Default: dry-run.",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Cap subscriptions processed."
    )
    parser.add_argument(
        "--subscription-ids",
        default=None,
        help="Comma-separated subscription UUIDs — a targeted set instead of the "
        "full cohort (still filtered by eligibility).",
    )
    args = parser.parse_args()

    sub_ids = (
        [s.strip() for s in args.subscription_ids.split(",") if s.strip()]
        if args.subscription_ids
        else None
    )

    db = SessionLocal()
    try:
        result = reconcile(db, apply=args.apply, sub_ids=sub_ids, limit=args.limit)
    finally:
        db.close()

    print(json.dumps(asdict(result), indent=2, default=str))
    mode = "APPLIED" if result.applied else "DRY-RUN"
    print(
        f"\n[{mode}] candidates={result.candidates} "
        f"{'restored' if result.applied else 'would_restore'}={result.restored} "
        f"lock_cleared_only={result.lock_cleared_only}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
