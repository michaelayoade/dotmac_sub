#!/usr/bin/env python
"""Report exactly what the prepaid balance sweep would do, without writes.

Examples (inside the app container):

    python scripts/one_off/plan_prepaid_balance_sweep.py
    python scripts/one_off/plan_prepaid_balance_sweep.py --limit 100 --details
    python scripts/one_off/plan_prepaid_balance_sweep.py --out /tmp/prepaid-plan.json

This command has no execute mode. Enabling the production control remains a
separate, explicit operator decision after the report is reviewed.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from app.db import SessionLocal
from app.services.prepaid_enforcement_planner import plan_prepaid_enforcement

SAMPLE_SIZE = 20


def _json_default(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--account-id",
        action="append",
        default=[],
        help="Repeatable subscriber UUID. Omit to inspect the full candidate cohort.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap the deterministic account cohort for a staged review.",
    )
    parser.add_argument(
        "--details",
        action="store_true",
        help="Print every planned account instead of a 20-row sample.",
    )
    parser.add_argument(
        "--out",
        metavar="PATH",
        default=None,
        help="Write the complete JSON plan to PATH.",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        plan = plan_prepaid_enforcement(
            db,
            account_ids=args.account_id or None,
            limit=args.limit,
        )
        summary = plan.to_dict(include_items=False)
        print("=== prepaid balance enforcement dry run ===")
        print(json.dumps(summary, indent=2, sort_keys=True, default=_json_default))

        rows = [item.to_dict() for item in plan.items]
        displayed = rows if args.details else rows[:SAMPLE_SIZE]
        if displayed:
            print(f"\n--- accounts (showing {len(displayed)} of {len(rows)}) ---")
            print(
                json.dumps(
                    displayed,
                    indent=2,
                    sort_keys=True,
                    default=_json_default,
                )
            )

        if args.out:
            with open(args.out, "w", encoding="utf-8") as output:
                json.dump(
                    plan.to_dict(),
                    output,
                    indent=2,
                    sort_keys=True,
                    default=_json_default,
                )
                output.write("\n")
            print(f"\nFull plan written: {args.out} ({len(rows)} accounts)")

        print(
            "\nDRY RUN ONLY - no timers, notices, service states, or sessions changed."
        )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
