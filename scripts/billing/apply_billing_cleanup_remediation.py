#!/usr/bin/env python
"""Apply reviewed billing-cleanup audit CSVs with a dry-run manifest gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.db import SessionLocal  # noqa: E402
from app.services.billing_cleanup_remediation import (  # noqa: E402
    apply_cleanup_remediation,
    load_cleanup_csv,
    plan_cleanup_remediation,
)


def _csv_sha256(path: str | None) -> str | None:
    if not path:
        return None
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def _input_fingerprints(args: argparse.Namespace) -> dict[str, str | None]:
    return {
        "stale_locks_csv_sha256": _csv_sha256(args.stale_locks_csv),
        "anchors_csv_sha256": _csv_sha256(args.anchors_csv),
        "mode_drift_csv_sha256": _csv_sha256(args.mode_drift_csv),
        "invoice_anchors_csv_sha256": _csv_sha256(args.invoice_anchors_csv),
        "prepaid_ar_csv_sha256": _csv_sha256(args.prepaid_ar_csv),
        "prepaid_overlaps_csv_sha256": _csv_sha256(args.prepaid_overlaps_csv),
        "disabled_lines_csv_sha256": _csv_sha256(args.disabled_lines_csv),
        "duplicate_lines_csv_sha256": _csv_sha256(args.duplicate_lines_csv),
        "orphan_addons_csv_sha256": _csv_sha256(args.orphan_addons_csv),
        "missing_radius_csv_sha256": _csv_sha256(args.missing_radius_csv),
    }


def _write(path: str | None, payload: dict) -> None:
    if not path:
        return
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
    print(f"manifest written: {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stale-locks-csv")
    parser.add_argument("--anchors-csv")
    parser.add_argument("--mode-drift-csv")
    parser.add_argument("--invoice-anchors-csv")
    parser.add_argument("--prepaid-ar-csv")
    parser.add_argument("--prepaid-overlaps-csv")
    parser.add_argument("--disabled-lines-csv")
    parser.add_argument("--duplicate-lines-csv")
    parser.add_argument("--orphan-addons-csv")
    parser.add_argument("--missing-radius-csv")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expect", help="Prior dry-run manifest required for --apply.")
    parser.add_argument("--out", help="Write result manifest here.")
    args = parser.parse_args()

    provided_csvs = [
        args.stale_locks_csv,
        args.anchors_csv,
        args.mode_drift_csv,
        args.invoice_anchors_csv,
        args.prepaid_ar_csv,
        args.prepaid_overlaps_csv,
        args.disabled_lines_csv,
        args.duplicate_lines_csv,
        args.orphan_addons_csv,
        args.missing_radius_csv,
    ]
    if not any(provided_csvs):
        print("REFUSED: provide at least one cleanup CSV.")
        return 2

    stale_rows = load_cleanup_csv(args.stale_locks_csv) if args.stale_locks_csv else []
    anchor_rows = load_cleanup_csv(args.anchors_csv) if args.anchors_csv else []
    mode_rows = load_cleanup_csv(args.mode_drift_csv) if args.mode_drift_csv else []
    invoice_anchor_rows = (
        load_cleanup_csv(args.invoice_anchors_csv) if args.invoice_anchors_csv else []
    )
    prepaid_ar_rows = (
        load_cleanup_csv(args.prepaid_ar_csv) if args.prepaid_ar_csv else []
    )
    prepaid_overlap_rows = (
        load_cleanup_csv(args.prepaid_overlaps_csv) if args.prepaid_overlaps_csv else []
    )
    disabled_line_rows = (
        load_cleanup_csv(args.disabled_lines_csv) if args.disabled_lines_csv else []
    )
    duplicate_line_rows = (
        load_cleanup_csv(args.duplicate_lines_csv) if args.duplicate_lines_csv else []
    )
    orphan_addon_rows = (
        load_cleanup_csv(args.orphan_addons_csv) if args.orphan_addons_csv else []
    )
    missing_radius_rows = (
        load_cleanup_csv(args.missing_radius_csv) if args.missing_radius_csv else []
    )
    fingerprints = _input_fingerprints(args)

    db = SessionLocal()
    try:
        plan = plan_cleanup_remediation(
            db,
            stale_lock_rows=stale_rows,
            anchor_rows=anchor_rows,
            mode_rows=mode_rows,
            invoice_anchor_rows=invoice_anchor_rows,
            prepaid_ar_rows=prepaid_ar_rows,
            prepaid_overlap_rows=prepaid_overlap_rows,
            disabled_line_rows=disabled_line_rows,
            duplicate_line_rows=duplicate_line_rows,
            orphan_addon_rows=orphan_addon_rows,
            missing_radius_rows=missing_radius_rows,
        )
        print("=== billing cleanup remediation plan ===")
        print(
            f"apply={plan['counts']['apply']} "
            f"skip={plan['counts']['skip']} refuse={plan['counts']['refuse']}"
        )
        print(f"by action: {plan['counts']['by_action']}")

        if not args.apply:
            result = apply_cleanup_remediation(db, plan, dry_run=True)
            _write(args.out, {**result, "counts": plan["counts"], **fingerprints})
            print("DRY RUN - nothing changed.")
            return 0

        if not args.expect:
            print("REFUSED: --apply requires --expect <prior dry-run manifest>.")
            return 2
        with open(args.expect, encoding="utf-8") as handle:
            expected = json.load(handle)
        for key, value in fingerprints.items():
            if expected.get(key) != value:
                print(f"REFUSED: {key} differs from prior dry-run manifest.")
                return 2
        expected_counts = expected.get("counts") or {}
        if expected_counts != plan["counts"]:
            print("REFUSED: current plan counts differ from prior dry-run manifest.")
            return 2

        result = apply_cleanup_remediation(db, plan, dry_run=False)
        _write(args.out, {**result, "counts": plan["counts"], **fingerprints})
        print(f"APPLIED: {result['applied_count']} changes, errors={result['errors']}")
        return 0 if result["errors"] == 0 else 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
