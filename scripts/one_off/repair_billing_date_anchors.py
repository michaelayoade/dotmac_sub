"""Repair subscriptions whose billing anchor is behind invoice coverage.

This is intentionally narrow: it only advances ``subscriptions.next_billing_at``
to the latest active invoice period end for the same subscription. It does not
create invoices, delete documents, change balances, or infer missing service
periods.

Dry-run by default; pass --apply to write guarded changes.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

from app.db import SessionLocal
from app.services.billing_cleanup_remediation import (
    apply_cleanup_remediation,
    discover_invoice_anchor_rows,
    plan_cleanup_remediation,
)

DEFAULT_OUTPUT = "scratchpad/billing_date_anchor_repair.csv"


def _write_csv(path: str, rows: list[dict[str, Any]]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    fieldnames: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                fieldnames.append(key)
                seen.add(key)
    with output.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write anchor repairs.")
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"CSV output path. Default: {DEFAULT_OUTPUT}",
    )
    parser.add_argument(
        "--json-output",
        default="",
        help="Optional JSON plan/result output path.",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        rows = discover_invoice_anchor_rows(db)
        _write_csv(args.output, rows)
        plan = plan_cleanup_remediation(db, invoice_anchor_rows=rows)
        result = apply_cleanup_remediation(db, plan, dry_run=not args.apply)
        payload = {
            "mode": "apply" if args.apply else "dry_run",
            "candidate_count": len(rows),
            "plan_counts": plan["counts"],
            "result": {
                "applied_count": result["applied_count"],
                "errors": result["errors"],
            },
            "csv_output": args.output,
        }
        if args.json_output:
            output = Path(args.json_output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(payload, indent=2, sort_keys=True))
        print(json.dumps(payload, indent=2, sort_keys=True))
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
