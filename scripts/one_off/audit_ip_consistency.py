#!/usr/bin/env python
"""Run the IPv4 consistency audit once and print the result as JSON.

Read-only. Quantifies how many active subscribers have an IPv4 that disagrees
across its three sources (subscription.ipv4_address column / IPAM IPAssignment /
external radreply Framed-IP). This is step 1 of the connectivity-reconciler
hardening — measure drift before refactoring writers. See
docs/FINANCIAL_ACCESS_ENFORCEMENT.md.

Usage (inside the app container so the DB + external RADIUS resolve):

    docker compose exec app python scripts/one_off/audit_ip_consistency.py
    docker compose exec app python scripts/one_off/audit_ip_consistency.py --store
    docker compose exec app python scripts/one_off/audit_ip_consistency.py --summary

--summary prints a compact, read-only table: count per kind, its alert tier
(critical/warning/info/drift) and sample IPs / logins with subscriber numbers.
No secrets are printed. Run it before loading ip_consistency.rules.yml.

--store also writes the result to Redis so the /metrics gauge
(radius_ip_consistency_drift) reflects this run immediately.
"""

import argparse
import json
import sys

from app.db import SessionLocal
from app.services.ip_consistency_audit import (
    _ALL_KINDS,
    CRITICAL_KINDS,
    INFORMATIONAL_KINDS,
    WARNING_KINDS,
    audit_ip_consistency,
    store_latest_ip_audit,
)


def format_summary(result: dict) -> str:
    """Counts per kind with samples (IPs, logins, subscriber numbers only)."""
    counts = result.get("counts", {})
    numbers = result.get("sample_subscribers", {})
    lines = [
        f"population={result.get('population')} errors={result.get('errors')} "
        f"ok={result.get('ok')}"
    ]
    for kind in _ALL_KINDS:
        if kind in CRITICAL_KINDS:
            tier = "CRITICAL"
        elif kind in WARNING_KINDS:
            tier = "warning"
        elif kind in INFORMATIONAL_KINDS:
            tier = "info"
        else:
            tier = "drift"
        lines.append(f"{kind:40s} {counts.get(kind, 0):6d}  [{tier}]")
        for sample in result.get(kind, [])[:5]:
            owners = numbers.get(kind, {}).get(sample)
            suffix = f"  subscribers={','.join(owners)}" if owners else ""
            lines.append(f"    {sample}{suffix}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--store",
        action="store_true",
        help="Also persist the result to Redis for the /metrics collector.",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print counts per kind with samples instead of the full JSON.",
    )
    args = parser.parse_args()

    session = SessionLocal()
    try:
        result = audit_ip_consistency(session)
    finally:
        session.close()

    if args.store:
        result["stored"] = store_latest_ip_audit(result)

    if args.summary:
        print(format_summary(result))
    else:
        print(json.dumps(result, indent=2, sort_keys=True))
    # Non-zero exit when drift or errors found, so it's usable as a check.
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
