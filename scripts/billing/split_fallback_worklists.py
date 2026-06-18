#!/usr/bin/env python
"""Split the annotated fallback-AR audit into finance work queues (READ-ONLY).

The single annotated sheet (post_cutover_fallback_postpaid_ar.csv) stays the
audit source of truth; this derives per-classification worklists for hand-off.
Pure stdlib — reads the two CSVs produced by audit_fallback_postpaid_ar.py, no
DB. See docs/POST_CUTOVER_HARDENING.md.

Outputs (repo root):
  post_cutover_ar_safe_no_action.csv     100 reconciled + 31 canceled/disabled
  post_cutover_ar_diff_review.csv         47 ledger_has_ar_but_differs
  post_cutover_missing_payments.csv       23 ledger_missing_payments
  post_cutover_postpaid_credit_review.csv  7 deposit_credit_on_postpaid
  post_cutover_unwall_risky.csv           23 risky active/blocked un-wall flips
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AR_SRC = ROOT / "post_cutover_fallback_postpaid_ar.csv"
UNWALL_SRC = ROOT / "post_cutover_fallback_unwall_diff.csv"

# classification -> output worklist filename
WORKLISTS = {
    "post_cutover_ar_safe_no_action.csv": {
        "ledger_reconciles",
        "canceled_or_disabled_review",
    },
    "post_cutover_ar_diff_review.csv": {"ledger_has_ar_but_differs"},
    "post_cutover_missing_payments.csv": {"ledger_missing_payments"},
    "post_cutover_postpaid_credit_review.csv": {"deposit_credit_on_postpaid"},
}

# Risky un-wall flips: active/blocked AND the AR can't be trusted.
RISKY_UNWALL_CLASSES = {"ledger_has_ar_but_differs", "ledger_missing_payments"}
RISKY_UNWALL_STATUSES = {"active", "blocked"}


def _write(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


def main() -> int:
    if not AR_SRC.exists() or not UNWALL_SRC.exists():
        print(
            f"source CSVs not found next to {ROOT} — run audit_fallback_postpaid_ar.py first"
        )
        return 1

    with open(AR_SRC, newline="") as fh:
        ar_rows = list(csv.DictReader(fh))
        ar_fields = ar_rows[0].keys() if ar_rows else []
    with open(UNWALL_SRC, newline="") as fh:
        unwall_rows = list(csv.DictReader(fh))
        unwall_fields = unwall_rows[0].keys() if unwall_rows else []

    print(f"source: {len(ar_rows)} AR rows, {len(unwall_rows)} un-wall flips\n")

    for fname, classes in WORKLISTS.items():
        subset = [r for r in ar_rows if r["classification"] in classes]
        _write(ROOT / fname, list(ar_fields), subset)
        print(f"  {fname:42s} {len(subset):>4}")

    risky = [
        r
        for r in unwall_rows
        if r["status"] in RISKY_UNWALL_STATUSES
        and r["classification"] in RISKY_UNWALL_CLASSES
    ]
    _write(ROOT / "post_cutover_unwall_risky.csv", list(unwall_fields), risky)
    print(f"  {'post_cutover_unwall_risky.csv':42s} {len(risky):>4}")

    total = sum(
        len([r for r in ar_rows if r["classification"] in cls])
        for cls in WORKLISTS.values()
    )
    print(f"\nAR worklists cover {total}/{len(ar_rows)} accounts (should equal total).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
