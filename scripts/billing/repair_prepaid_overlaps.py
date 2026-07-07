#!/usr/bin/env python
"""Audit or repair prepaid invoices that overlap already-paid coverage."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.services.billing_prepaid_overlap_repair import (
    find_prepaid_overlap_candidates,
    repair_prepaid_overlapping_invoices,
    write_prepaid_overlap_report,
)
from app.services.db_session_adapter import db_session_adapter


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply repairs. Without this flag the command is a dry-run.",
    )
    parser.add_argument(
        "--sync-radius",
        action="store_true",
        help="After applying repairs, re-sync restored subscriptions to RADIUS.",
    )
    parser.add_argument(
        "--csv",
        default="scratchpad/prepaid_overlap_repair_report.csv",
        help="Path for the before/after CSV report.",
    )
    args = parser.parse_args()

    session = db_session_adapter.create_session()
    try:
        candidates = find_prepaid_overlap_candidates(session)
        write_prepaid_overlap_report(candidates, Path(args.csv))
        result = repair_prepaid_overlapping_invoices(
            session, apply=args.apply, sync_radius=args.sync_radius
        )
        print(json.dumps({k: v for k, v in result.items() if k != "report"}, indent=2))
        print(f"report: {args.csv}")
        return 0
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
