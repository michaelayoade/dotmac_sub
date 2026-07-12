"""Report redacted historical access-path evidence for blocked NAS lifecycle rows."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from dotenv import load_dotenv

from app.db import SessionLocal
from app.services.nas_access_path_evidence import (
    build_nas_access_path_evidence_report,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--details", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    load_dotenv()
    args = _parser().parse_args(argv)
    db = SessionLocal()
    try:
        try:
            report = build_nas_access_path_evidence_report(
                db,
                window_days=args.days,
            )
        except Exception as exc:
            db.rollback()
            print(
                json.dumps(
                    {
                        "status": "error",
                        "error_type": type(exc).__name__,
                        "error": "NAS access-path evidence report failed.",
                    }
                ),
                file=sys.stderr,
            )
            return 1
        print(json.dumps(report.as_dict(include_details=args.details), sort_keys=True))
        return 0 if report.accounting_source_fresh else 2
    finally:
        db.rollback()
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
