#!/usr/bin/env python
"""Reconcile stale subscriber-level block drift (dry-run first).

Cohort: subscribers ``status='blocked'`` whose subscriptions are ALL active —
denormalization drift that walls the customer at the BNG despite an active
service (see app/services/account_status_reconcile.py). Re-derives the account
status from its subscriptions, then rebuilds RADIUS once and CoA-kicks the
affected sessions. Mixed-status accounts are deliberately NOT touched.

NO ledger / money writes.

Usage (inside the app container; PYTHONPATH=/app):

    # dry run — prints the cohort + projected status, writes nothing (default)
    docker compose exec -T -e PYTHONPATH=/app app \
        python scripts/one_off/reconcile_blocked_subscriber_drift.py

    # apply, capped to the first 25 accounts (start small)
    docker compose exec -T -e PYTHONPATH=/app app \
        python scripts/one_off/reconcile_blocked_subscriber_drift.py --apply --limit 25

    # apply everything in the cohort
    docker compose exec -T -e PYTHONPATH=/app app \
        python scripts/one_off/reconcile_blocked_subscriber_drift.py --apply
"""

import argparse
import json
import sys
from dataclasses import asdict

from app.db import SessionLocal
from app.services.account_status_reconcile import reconcile_cohort

SAMPLE = 15


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the status changes + refresh RADIUS + CoA. Default: dry-run.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap the number of accounts reconciled. Ignored if --account-ids/"
        "--account-id is given.",
    )
    parser.add_argument(
        "--account-ids",
        default=None,
        help="Comma-separated subscriber UUIDs — a TARGETED sample instead of the "
        "first-N cohort. Still filtered by eligibility (blocked + all-active); "
        "ineligible ones are reported as skipped with a reason and never mutated. "
        "Takes precedence over --limit.",
    )
    parser.add_argument(
        "--account-id",
        action="append",
        default=[],
        dest="account_id",
        metavar="UUID",
        help="Repeatable single subscriber UUID; combines with --account-ids.",
    )
    parser.add_argument(
        "--no-coa",
        action="store_true",
        help="Skip the CoA session kick (status + RADIUS rebuild only).",
    )
    parser.add_argument(
        "--out",
        metavar="PATH",
        default=None,
        help=(
            "Write the FULL result (every account_id + prior/new status), not "
            "just the printed sample, to PATH as JSON — an audit artifact of "
            "exactly which subscribers were reconciled. Recommended before the "
            "full apply (export the dry-run and the --limit sample first)."
        ),
    )
    args = parser.parse_args()

    # Collect targeted ids from both flags (precedence over --limit).
    account_ids = [a.strip() for a in (args.account_ids or "").split(",") if a.strip()]
    account_ids += [a.strip() for a in args.account_id if a.strip()]
    account_ids = account_ids or None
    if account_ids and args.limit is not None:
        print("NOTE: --limit is ignored because --account-ids/--account-id was given.")

    session = SessionLocal()
    try:
        summary = reconcile_cohort(
            session,
            account_ids=account_ids,
            limit=args.limit,
            dry_run=not args.apply,
            send_coa=not args.no_coa,
        )

        report = {
            "mode": "dry_run" if summary.dry_run else "apply",
            "targeted": account_ids is not None,
            "candidates": summary.candidates,
            "changed": summary.changed,
            "errors": summary.errors,
            "skipped": len(summary.skipped),
            "radius_refreshed": summary.radius_refreshed,
            "sessions_kicked": summary.sessions_kicked,
        }
        print("=== blocked-but-all-active subscriber drift ===")
        print(json.dumps(report, indent=2, sort_keys=True))

        if summary.skipped:
            print(f"\n--- skipped (ineligible, {len(summary.skipped)}) ---")
            print(json.dumps(summary.skipped, indent=2))

        results = [asdict(r) for r in summary.results]
        sample = results[:SAMPLE]
        if sample:
            print(f"\n--- sample (showing {len(sample)} of {len(results)}) ---")
            print(json.dumps(sample, indent=2))

        if args.out:
            with open(args.out, "w") as fh:
                json.dump(
                    {**report, "skipped_detail": summary.skipped, "results": results},
                    fh,
                    indent=2,
                    sort_keys=True,
                )
            print(f"\nFull audit artifact written: {args.out} ({len(results)} rows)")

        if summary.dry_run:
            print("\nDRY RUN — no changes written. Re-run with --apply to reconcile.")
        return 0 if summary.errors == 0 else 1
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
