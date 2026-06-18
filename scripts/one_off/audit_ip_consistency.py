#!/usr/bin/env python
"""Run the IPv4 consistency audit once and print the result as JSON.

Read-only. Quantifies how many active subscribers have an IPv4 that disagrees
across its three sources (subscription.ipv4_address column / IPAM IPAssignment /
external radreply Framed-IP). This is step 1 of the connectivity-reconciler
hardening — measure drift before refactoring writers. See
docs/designs/SERVICE_LIFECYCLE_BUNDLE_INTEGRITY.md.

Usage (inside the app container so the DB + external RADIUS resolve):

    docker compose exec app python scripts/one_off/audit_ip_consistency.py
    docker compose exec app python scripts/one_off/audit_ip_consistency.py --store

--store also writes the result to Redis so the /metrics gauge
(radius_ip_consistency_drift) reflects this run immediately.
"""

import argparse
import json
import sys

from app.db import SessionLocal
from app.services.ip_consistency_audit import (
    audit_ip_consistency,
    store_latest_ip_audit,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--store",
        action="store_true",
        help="Also persist the result to Redis for the /metrics collector.",
    )
    args = parser.parse_args()

    session = SessionLocal()
    try:
        result = audit_ip_consistency(session)
    finally:
        session.close()

    if args.store:
        result["stored"] = store_latest_ip_audit(result)

    print(json.dumps(result, indent=2, sort_keys=True))
    # Non-zero exit when drift or errors found, so it's usable as a check.
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
