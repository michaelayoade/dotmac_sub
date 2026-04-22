#!/usr/bin/env python3
"""Backfill legacy ONT desired state into bundle assignment + sparse overrides."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import SessionLocal
from app.services.network.ont_bundle_backfill import run_backfill


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Convert legacy ONT desired state into one active bundle assignment "
            "plus sparse overrides."
        )
    )
    parser.add_argument("--ont-id", help="Limit execution to a single ONT id")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit the number of candidate ONTs processed",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist backfillable plans. Default is dry-run.",
    )
    parser.add_argument(
        "--show",
        type=int,
        default=20,
        help="Maximum number of per-ONT rows to print",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    db = SessionLocal()
    try:
        result = run_backfill(
            db,
            ont_id=args.ont_id,
            limit=args.limit,
            apply=args.apply,
        )

        print("ONT bundle backfill")
        print(f"  mode: {'apply' if args.apply else 'dry-run'}")
        print(f"  already_migrated: {result.counts.get('already_migrated', 0)}")
        print(f"  backfill: {result.counts.get('backfill', 0)}")
        print(f"  manual_review: {result.counts.get('manual_review', 0)}")
        print(f"  unconfigured: {result.counts.get('unconfigured', 0)}")

        print("")
        for plan in result.plans[: max(args.show, 0)]:
            bundle_part = f" bundle={plan.bundle_name or plan.bundle_id}" if plan.bundle_id else ""
            print(
                f"- {plan.serial_number or plan.ont_id}: "
                f"{plan.outcome} | {plan.reason}{bundle_part}"
            )
            if plan.override_values:
                override_names = ", ".join(sorted(plan.override_values))
                print(f"    overrides: {override_names}")
            if plan.warnings:
                print(f"    warnings: {'; '.join(plan.warnings)}")

        if args.apply:
            db.commit()
        else:
            db.rollback()
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
