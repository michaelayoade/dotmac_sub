#!/usr/bin/env python3
"""Reconcile ``OLTDevice.is_active`` drift against the owned lifecycle status.

Preview by default (read-only). ``--apply`` repairs the drift through the owner
(``olt_active_reconcile.reconcile_active_flag``), emitting an ``olt.updated``
audit event per change. This is the auditable, idempotent alternative to an
ad-hoc ``UPDATE olt_devices SET is_active=...``.

    # preview which OLTs would be reactivated (and any blocked ones)
    python scripts/network/reconcile_olt_active_flag.py

    # apply the reconciliation
    python scripts/network/reconcile_olt_active_flag.py --apply
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
from app.services.network.olt_lifecycle import (  # noqa: E402
    preview_active_drift,
    reconcile_active_flag,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the reconciliation (default: read-only preview).",
    )
    parser.add_argument("--actor", default="reconcile_olt_active_flag")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        if args.apply:
            result = reconcile_active_flag(db, actor=args.actor, apply=True)
        else:
            drift = preview_active_drift(db)
            result = {
                "preview": True,
                "candidates": len(drift),
                "reconcilable": sum(1 for d in drift if d.can_activate),
                "drift": [asdict(d) for d in drift],
            }
    finally:
        db.close()

    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
