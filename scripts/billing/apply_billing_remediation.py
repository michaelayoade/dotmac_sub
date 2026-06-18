#!/usr/bin/env python
"""Apply finance's APPROVED billing-violation dispositions — strictly gated.

The most dangerous tool in the hardening: it mutates money records. Every guard
in app/services/billing_remediation.py is load-bearing. See
docs/POST_CUTOVER_BILLING_VIOLATIONS.md.

DEFAULT IS DRY-RUN. ``--apply`` requires ``--expect`` (a prior dry-run manifest)
and refuses if the plan drifted since that dry-run — there is NO override.

Usage:
    # 1. dry-run against the finance-approved CSV (writes nothing)
    docker compose exec -T -e PYTHONPATH=/app app \
        python scripts/billing/apply_billing_remediation.py \
            --csv /app/approved_dispositions.csv --out /app/remediation_plan.json

    # 2. apply, gated on the reviewed dry-run manifest
    docker compose exec -T -e PYTHONPATH=/app app \
        python scripts/billing/apply_billing_remediation.py \
            --csv /app/approved_dispositions.csv --apply \
            --expect /app/remediation_plan.json --out /app/remediation_applied.json

    # rollback exactly what an apply manifest changed
    docker compose exec -T -e PYTHONPATH=/app app \
        python scripts/billing/apply_billing_remediation.py \
            --rollback /app/remediation_applied.json
"""

from __future__ import annotations

import argparse
import json
import sys

from app.db import SessionLocal
from app.services.billing_remediation import (
    apply_remediation,
    load_disposition_csv,
    plan_remediation,
    rollback_remediation,
)


def _write(path, payload):
    if path:
        with open(path, "w") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
        print(f"manifest written: {path}")


def _apply_before_map(items):
    return {i["invoice_line_id"]: i.get("before") for i in items if i.get("before")}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--csv", help="finance-approved disposition CSV (#287 + action col)")
    p.add_argument("--apply", action="store_true", help="write. Requires --expect.")
    p.add_argument("--expect", help="prior dry-run manifest to gate --apply against")
    p.add_argument("--out", help="write the result manifest here")
    p.add_argument("--rollback", help="apply manifest whose changes to reverse")
    args = p.parse_args()

    db = SessionLocal()
    try:
        if args.rollback:
            with open(args.rollback) as fh:
                manifest = json.load(fh)
            res = rollback_remediation(db, manifest)
            print(f"ROLLBACK: reversed {res['reversed']} changes")
            return 0

        if not args.csv:
            print("REFUSED: --csv (approved disposition CSV) required.")
            return 2
        rows = load_disposition_csv(args.csv)
        plan = plan_remediation(db, rows)
        c = plan["counts"]
        print("=== billing remediation plan ===")
        print(
            f"  rows={len(rows)}  apply={c['apply']}  skip={c['skip']}  refuse={c['refuse']}"
        )
        print(f"  by action: {c['by_action']}")
        refusals = [
            (i["invoice_line_id"], i["action"], i["reason"])
            for i in plan["items"]
            if i["decision"] == "refuse"
        ]
        if refusals:
            print("  -- refused --")
            for lid, act, reason in refusals[:30]:
                print(f"    {lid}  {act}  {reason}")

        if not args.apply:
            dry = apply_remediation(db, plan, dry_run=True)
            _write(args.out, {**dry, "counts": c})
            print("\nDRY RUN — nothing changed.")
            return 0

        # --- apply (gated) ---
        if not args.expect:
            print("REFUSED: --apply requires --expect <prior dry-run manifest>.")
            return 2
        with open(args.expect) as fh:
            expected = json.load(fh)
        cur = _apply_before_map(i for i in plan["items"] if i["decision"] == "apply")
        exp = _apply_before_map(expected.get("applied", []))
        if cur != exp:
            print(
                "REFUSED: plan drifted since the reviewed dry-run (invoice/line "
                "state changed). Re-run dry-run and re-review. No override."
            )
            return 3

        res = apply_remediation(db, plan, dry_run=False)
        _write(args.out, {**res, "counts": c})
        print(f"\nAPPLIED {res['applied_count']} (errors={res['errors']}).")
        print("ROLLBACK: re-run with --rollback <this manifest> to reverse.")
        return 0 if res["errors"] == 0 else 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
