#!/usr/bin/env python3
"""Export and apply cutover reconstructed-balance corrections.

The reconstruction formula is:

    Splynx cutover balance
  + payments received since cutover
  - services consumed / charged since cutover
  + ordinary post-cutover adjustments

Dry-run is the default. ``apply-corrections`` writes internal ``Correction:``
ledger rows only for approved items; those rows are excluded from customer-facing
statements by the canonical ledger.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from app.db import SessionLocal
from app.services.cutover_balance_audit import (
    apply_reconstructed_balance_corrections,
    build_reconstructed_balance_correction_items,
    export_reconstructed_balance_packet,
    write_reconstructed_balance_correction_report,
)


def _export(args: argparse.Namespace) -> int:
    db = SessionLocal()
    try:
        manifest = export_reconstructed_balance_packet(db, Path(args.out_dir))
        print(
            "cutover reconstruction exported: "
            f"population={manifest['population']} "
            f"drift_count={manifest['drift_count']} "
            f"overcredited={manifest['overcredited_count']} "
            f"understated={manifest['understated_count']} "
            f"out_dir={args.out_dir}"
        )
        return 0
    finally:
        db.close()


def _apply_corrections(args: argparse.Namespace) -> int:
    db = SessionLocal()
    try:
        items = build_reconstructed_balance_correction_items(
            db,
            apply_overcredited=args.apply_overcredited,
            snapshot_date=args.snapshot_date,
        )
        payload = apply_reconstructed_balance_corrections(
            db,
            items,
            apply=args.apply,
        )
        if args.apply:
            db.commit()
        else:
            db.rollback()
        write_reconstructed_balance_correction_report(
            payload,
            json_path=Path(args.json_out),
            csv_path=Path(args.csv_out),
        )
        counts = payload["counts"]
        print(
            "cutover corrections "
            f"{'applied' if args.apply else 'dry-run'}: "
            f"total={counts['total']} apply={counts['apply']} "
            f"hold={counts['hold']} skip={counts['skip']} "
            f"credit_customer_amount={counts['credit_customer_amount']} "
            f"debit_customer_amount={counts['debit_customer_amount']} "
            f"held_overcredited_amount={counts['held_overcredited_amount']}"
        )
        print(f"json={args.json_out}")
        print(f"csv={args.csv_out}")
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    export_parser = subparsers.add_parser("export")
    export_parser.add_argument(
        "--out-dir",
        default="scratchpad/cutover_reconstructed_statements_current",
    )
    export_parser.set_defaults(func=_export)

    apply_parser = subparsers.add_parser("apply-corrections")
    apply_parser.add_argument("--apply", action="store_true", help="write changes")
    apply_parser.add_argument(
        "--apply-overcredited",
        action="store_true",
        help="also debit overcredited accounts; requires finance approval",
    )
    apply_parser.add_argument(
        "--snapshot-date",
        default=None,
        help="date tag for correction memos, default today in UTC",
    )
    apply_parser.add_argument(
        "--json-out",
        default="scratchpad/cutover_reconstructed_balance_corrections_current.json",
    )
    apply_parser.add_argument(
        "--csv-out",
        default="scratchpad/cutover_reconstructed_balance_corrections_current.csv",
    )
    apply_parser.set_defaults(func=_apply_corrections)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
