#!/usr/bin/env python
"""Build a read-only billing cleanup audit after prepaid/postpaid drift fixes."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services.billing_cleanup_audit import (  # noqa: E402
    build_billing_cleanup_report,
    write_billing_cleanup_report,
)
from app.services.db_session_adapter import db_session_adapter  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default="scratchpad/billing_cleanup_audit",
        help="Directory for summary.json and per-bucket CSV files.",
    )
    args = parser.parse_args()

    session = db_session_adapter.create_session()
    try:
        report = build_billing_cleanup_report(session)
        files = write_billing_cleanup_report(report, Path(args.out))
        print(json.dumps(report.summary(), indent=2, sort_keys=True))
        print(f"output_dir: {args.out}")
        for name, path in files.items():
            print(f"{name}: {path}")
        return 0
    finally:
        session.rollback()
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
