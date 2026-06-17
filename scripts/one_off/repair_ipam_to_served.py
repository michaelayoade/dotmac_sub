#!/usr/bin/env python
"""Step 2a — repair the IPAM ledger to match the served IPv4 (dry-run first).

Makes ``IPAssignment`` reflect what RADIUS actually serves
(``subscription.ipv4_address`` == radreply Framed-IP). NEVER changes a served
IP — it only backfills/repoints the ledger, so it is non-customer-impacting.
Refuses to auto-fix conflicts (served IP already assigned to another subscriber,
management/ONT address, or a subscriber claiming two IPs). See
docs/designs/SERVICE_LIFECYCLE_BUNDLE_INTEGRITY.md §5b.

Usage (inside the app container; PYTHONPATH=/app so `app` resolves):

    # dry run — prints the plan, writes nothing (default)
    docker compose exec -T -e PYTHONPATH=/app app \
        python scripts/one_off/repair_ipam_to_served.py

    # apply, capped to the first 25 subscribers (start small)
    docker compose exec -T -e PYTHONPATH=/app app \
        python scripts/one_off/repair_ipam_to_served.py --apply --limit 25

    # apply everything actionable
    docker compose exec -T -e PYTHONPATH=/app app \
        python scripts/one_off/repair_ipam_to_served.py --apply
"""

import argparse
import json
import sys

from app.db import SessionLocal
from app.services.ip_assignment_repair import (
    ACTIONABLE,
    CONFLICTS,
    apply_repair,
    plan_repair,
)

SAMPLE = 15


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the repairs. Without this, dry-run (default).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap the number of subscribers repaired (only with --apply).",
    )
    args = parser.parse_args()

    session = SessionLocal()
    try:
        plan = plan_repair(session)

        summary = {
            "population": plan["population"],
            "counts": plan["counts"],
            "actionable": plan["actionable"],
            "conflicts": plan["conflicts"],
        }
        print("=== IPAM-to-served repair plan ===")
        print(json.dumps(summary, indent=2, sort_keys=True))

        # Samples per action so the operator can eyeball before applying.
        for action in (*ACTIONABLE, *CONFLICTS):
            sample = [
                {"subscriber_id": it["subscriber_id"], "desired_ip": it["desired_ip"],
                 "current": it["current_ipam_ips"]}
                for it in plan["items"]
                if it["action"] == action
            ][:SAMPLE]
            if sample:
                print(f"\n--- {action} (showing {len(sample)}) ---")
                print(json.dumps(sample, indent=2))

        if not args.apply:
            print("\nDRY RUN — no changes written. Re-run with --apply to repair.")
            return 0

        print(f"\nAPPLYING (limit={args.limit})...")
        applied = apply_repair(session, plan, limit=args.limit)
        print(json.dumps(applied, indent=2, sort_keys=True))
        return 0 if applied["errors"] == 0 else 1
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
