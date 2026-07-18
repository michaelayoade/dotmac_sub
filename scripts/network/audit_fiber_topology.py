"""Print the canonical, read-only fiber-topology integrity report."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from app.db import SessionLocal  # noqa: E402
from app.services.fiber_topology import audit_fiber_topology  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit OLT-to-customer and passive-fiber topology integrity."
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Emit compact JSON instead of indented JSON.",
    )
    parser.add_argument(
        "--verify-customer-traces",
        action="store_true",
        help=(
            "Resolve every active fiber subscription before evaluating the "
            "customer-trace evidence completeness gate. This can be expensive."
        ),
    )
    parser.add_argument(
        "--trace-limit",
        type=int,
        default=None,
        help=(
            "Bound a shadow trace sample. A limited run is never complete "
            "even when every sampled path is complete."
        ),
    )
    args = parser.parse_args()
    if args.trace_limit is not None and not args.verify_customer_traces:
        parser.error("--trace-limit requires --verify-customer-traces")
    if args.trace_limit is not None and args.trace_limit < 1:
        parser.error("--trace-limit must be at least 1")
    return args


def main() -> int:
    args = parse_args()
    with SessionLocal() as db:
        report = audit_fiber_topology(
            db,
            verify_customer_traces=args.verify_customer_traces,
            trace_limit=args.trace_limit,
        )
    print(
        json.dumps(
            report.to_dict(),
            default=str,
            indent=None if args.compact else 2,
            sort_keys=True,
        )
    )
    return 0 if report.customer_trace_evidence_complete else 2


if __name__ == "__main__":
    raise SystemExit(main())
