#!/usr/bin/env python
"""Cross-domain billing-integrity audit (READ-ONLY).

Runs every registered invariant check (billing × lifecycle × IPAM × RADIUS) and
prints counts + samples + the launch-gate verdict. Writes nothing. See
docs/POST_CUTOVER_HARDENING.md.

Usage (in the app container):
    docker compose exec -T -e PYTHONPATH=/app app \
        python scripts/billing/billing_integrity_audit.py
"""

from __future__ import annotations

import json
import sys

from app.db import SessionLocal
from app.services.billing_integrity_audit import (
    _LAUNCH_BLOCKING,
    audit_billing_integrity,
)


def main() -> int:
    db = SessionLocal()
    try:
        result = audit_billing_integrity(db)
    finally:
        db.close()

    print("=== billing integrity audit (READ-ONLY) ===")
    for name, res in result["checks"].items():
        flag = " [LAUNCH-BLOCKING]" if name in _LAUNCH_BLOCKING else ""
        err = " ERROR" if res.get("error") or res.get("errors") else ""
        print(f"  {name:46s} {res.get('count', 0):>6}{flag}{err}")
        if res.get("samples"):
            print(f"      samples: {json.dumps(res['samples'][:8])}")
    print(f"\nlaunch_blocked: {result['launch_blocked']}  errors: {result['errors']}")
    print(
        "  (billing automation must NOT launch while any LAUNCH-BLOCKING gauge is "
        "non-zero)"
    )
    # Exit non-zero if any check tripped, so this is usable as a gate.
    return 0 if not result["launch_blocked"] and result["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
