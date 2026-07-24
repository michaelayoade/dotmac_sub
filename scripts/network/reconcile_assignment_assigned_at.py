#!/usr/bin/env python3
"""Backfill assigned_at on active ONT assignments missing it.

Preview by default (read-only). ``--apply`` sets ``assigned_at = created_at``
through the ONT-assignment command owner, staging an audit event per change.
This is the repair for the pure-timestamp case, which the governed identity
``canonicalize`` batch rejects as "already canonical".

    python scripts/network/reconcile_assignment_assigned_at.py            # preview
    python scripts/network/reconcile_assignment_assigned_at.py --apply
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from app.db import SessionLocal  # noqa: E402
from app.services.network.ont_assignment_commands import (  # noqa: E402
    preview_assigned_at_drift,
    reconcile_assigned_at,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the backfill (default: read-only preview).",
    )
    parser.add_argument("--actor", default="reconcile_assignment_assigned_at")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        if args.apply:
            result = reconcile_assigned_at(db, actor=args.actor, apply=True)
        else:
            drift = preview_assigned_at_drift(db)
            result = {
                "preview": True,
                "candidates": len(drift),
                "sample": [asdict(d) for d in drift[:10]],
            }
    finally:
        db.close()

    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
