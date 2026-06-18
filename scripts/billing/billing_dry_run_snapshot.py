#!/usr/bin/env python
"""Daily dry-run billing snapshot (READ-ONLY).

Runs ``run_invoice_cycle(dry_run=True)`` (which runs even while
``billing_enabled`` is false) and captures the operational counts for the
launch discipline (see docs/BILLING_AUTOMATION_LAUNCH_RUNBOOK.md, Step 3).

The dry run does NOT commit, but it CAN dirty ORM objects in the session before
the dry-run branch (notably fast-forwarding ``subscription.next_billing_at``,
billing_automation.py:855). This CLI therefore ``db.rollback()`` after the run:
its contract is that it leaves NO committed changes. Verified by
``tests/test_billing_dry_run_snapshot.py``.

Pair with the safety gauges from
``scripts/billing/billing_integrity_audit.py`` — disabled/canceled-billed and
duplicate-period counts MUST be zero before automation launches.

``--out`` writes the snapshot JSON; ``--prev`` prints the day-over-day delta.
Any unexplained delta is a stop.

Usage:
    docker compose exec -T -e PYTHONPATH=/app app \
        python scripts/billing/billing_dry_run_snapshot.py \
            --out /app/billing_dryrun_$(date +%F).json --prev /app/billing_dryrun_prev.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

from app.db import SessionLocal
from app.services.billing_automation import run_invoice_cycle

_FIELDS = (
    "subscriptions_scanned",
    "subscriptions_billed",
    "lines_created",
    "pending_activated",
    "skipped",
    "currency_skipped",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", help="Write the snapshot JSON here.")
    parser.add_argument("--prev", help="Prior snapshot JSON to diff against.")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        summary = run_invoice_cycle(db, dry_run=True)
        # Defensive: discard any pending (uncommitted) objects the cycle built.
        db.rollback()
    finally:
        db.close()

    snapshot = {"generated_at": datetime.now(UTC).isoformat(), "dry_run": True}
    for f in _FIELDS:
        snapshot[f] = int(summary.get(f, 0) or 0)

    print("=== billing dry-run snapshot (READ-ONLY) ===")
    for f in _FIELDS:
        print(f"  {f:24s} {snapshot[f]:>7}")
    print(
        "\nNOTE: revenue total is reconciled by finance from subscriptions_billed; "
        "safety gauges (disabled/canceled-billed, duplicate-period) come from "
        "billing_integrity_audit.py — both must be acceptable before launch."
    )

    if args.prev:
        try:
            with open(args.prev) as fh:
                prev = json.load(fh)
            print("\n-- delta vs previous --")
            for f in _FIELDS:
                d = snapshot[f] - int(prev.get(f, 0) or 0)
                flag = "  <-- CHANGED" if d else ""
                print(f"  {f:24s} {d:+7d}{flag}")
        except (OSError, json.JSONDecodeError) as exc:
            print(f"\n(could not read --prev: {exc})")

    if args.out:
        with open(args.out, "w") as fh:
            json.dump(snapshot, fh, indent=2, sort_keys=True)
        print(f"\nsnapshot written: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
