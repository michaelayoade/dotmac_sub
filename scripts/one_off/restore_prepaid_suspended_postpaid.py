#!/usr/bin/env python
"""Restore postpaid services wrongly suspended by prepaid balance enforcement.

Companion remediation to the PrepaidEnforcement scope fix. Clears the wrongful
``prepaid`` enforcement lock from postpaid subscriptions and reactivates those
held by nothing else. Conservative: accounts with overdue debt are skipped; a
sub still held by another lock (e.g. a stale ``overdue`` lock) has its prepaid
lock cleared but is NOT force-activated (reported for the dunning flow). See
app/services/prepaid_scope_repair.py and
scripts/one_off/audit_prepaid_suspended_postpaid.sql.

NO ledger / money writes.

Usage (inside the app container; PYTHONPATH=/app):

    # dry run — prints the cohort + projected outcome, writes nothing (default)
    docker compose exec -T -e PYTHONPATH=/app app \
        python scripts/one_off/restore_prepaid_suspended_postpaid.py

    # apply, capped to a few accounts first
    docker compose exec -T -e PYTHONPATH=/app app \
        python scripts/one_off/restore_prepaid_suspended_postpaid.py --apply --limit 3

    # target specific subscriptions
    docker compose exec -T -e PYTHONPATH=/app app \
        python scripts/one_off/restore_prepaid_suspended_postpaid.py --apply \
        --subscription-ids <uuid>,<uuid>
"""

import argparse
import json
import sys
from dataclasses import asdict

from app.db import SessionLocal
from app.services.prepaid_scope_repair import repair


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Clear the locks + reactivate. Default: dry-run (writes nothing).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap the number of subscriptions processed.",
    )
    parser.add_argument(
        "--subscription-ids",
        default=None,
        help="Comma-separated subscription UUIDs — a targeted set instead of the "
        "full cohort. Still filtered by eligibility (postpaid + suspended + "
        "active prepaid lock); ineligible ones are simply not matched.",
    )
    args = parser.parse_args()

    sub_ids = (
        [s.strip() for s in args.subscription_ids.split(",") if s.strip()]
        if args.subscription_ids
        else None
    )

    db = SessionLocal()
    try:
        result = repair(db, apply=args.apply, sub_ids=sub_ids, limit=args.limit)
    finally:
        db.close()

    print(json.dumps(asdict(result), indent=2, default=str))
    mode = "APPLIED" if result.applied else "DRY-RUN"
    print(
        f"\n[{mode}] candidates={result.candidates} "
        f"{'restored' if result.applied else 'would_restore'}={result.restored} "
        f"lock_cleared_only={result.lock_cleared_only} skipped={result.skipped}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
