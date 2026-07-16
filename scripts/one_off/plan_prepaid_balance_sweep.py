#!/usr/bin/env python
"""Report exactly what the prepaid balance sweep would do, without writes.

Examples (inside the app container):

    python scripts/one_off/plan_prepaid_balance_sweep.py
    python scripts/one_off/plan_prepaid_balance_sweep.py --limit 100 --details
    python scripts/one_off/plan_prepaid_balance_sweep.py --out /tmp/prepaid-plan.json
    python scripts/one_off/plan_prepaid_balance_sweep.py \
      --activation-at 2026-07-20T08:00:00+01:00

Funding always comes from the same materialized reconstruction owner consumed
by execution; the command accepts no alternate balance snapshot. The optional
``--record-readiness`` mode records the reviewed live-owner plan as cutover
evidence. It does not enable enforcement or change money, timers, notices,
service state, or sessions.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from typing import Any

from app.db import SessionLocal
from app.services.prepaid_enforcement_planner import plan_prepaid_enforcement

SAMPLE_SIZE = 20


def _json_default(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _parse_datetime(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty ISO 8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone offset")
    return parsed


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
    parser.add_argument(
        "--activation-at",
        default=None,
        help=(
            "Preview a proposed ISO 8601 activation time. This does not persist "
            "the setting or enable enforcement."
        ),
    )
    parser.add_argument(
        "--record-readiness",
        action="store_true",
        help="Persist verified full-cohort cutover evidence; never enables the feature.",
    )
    parser.add_argument(
        "--evidence-ref",
        default=None,
        help="Non-secret reference to the reconstruction/bank evidence package.",
    )
    parser.add_argument(
        "--verified-by",
        default=None,
        help="Operator identity recorded with the readiness evidence.",
    )
    args = parser.parse_args()

    activation_at = (
        _parse_datetime(args.activation_at, field="activation_at")
        if args.activation_at
        else None
    )
    if args.record_readiness:
        if activation_at is None:
            parser.error("--record-readiness requires --activation-at")
        if args.account_id or args.limit is not None:
            parser.error("--record-readiness requires the complete candidate cohort")
        if not args.evidence_ref or not args.verified_by:
            parser.error("--record-readiness requires --evidence-ref and --verified-by")

    db = SessionLocal()
    try:
        plan = plan_prepaid_enforcement(
            db,
            account_ids=args.account_id or None,
            limit=args.limit,
            activation_at=activation_at,
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

        if args.record_readiness:
            from app.services.prepaid_enforcement_readiness import (
                record_prepaid_enforcement_readiness,
            )

            assert activation_at is not None
            record = record_prepaid_enforcement_readiness(
                db,
                activation_at=activation_at,
                evidence_ref=args.evidence_ref,
                verified_by=args.verified_by,
                now=plan.generated_at,
            )
            db.commit()
            print(
                "\nReadiness recorded: "
                f"id={record.id} accounts={record.candidate_account_count} "
                f"currency={record.currency}"
            )

        if args.record_readiness:
            print("No financial or customer enforcement state was changed.")
        else:
            print(
                "\nDRY RUN ONLY - no timers, notices, service states, or sessions changed."
            )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
