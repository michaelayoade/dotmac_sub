#!/usr/bin/env python3
"""Print a read-only full-population billing numbering seed proposal; never apply it."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from datetime import date, datetime
from uuid import UUID

from dotmac_kernel.settings_cache import active_settings_cache, install_settings_cache
from sqlalchemy.exc import SQLAlchemyError

from app.db import read_only_snapshot_session
from app.services.billing_numbering_readiness import (
    NumberingReadinessError,
    NumberingReadinessQuery,
    billing_numbering_readiness_report,
)


def _json_scalar(value: object) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    raise TypeError(f"unsupported report value: {type(value).__name__}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-date", type=date.fromisoformat, required=True)
    args = parser.parse_args(argv)
    query = NumberingReadinessQuery(reference_date=args.reference_date)
    previous_store = active_settings_cache()
    try:
        # Standalone process only. Canonical resolver reads this snapshot;
        # no shared Redis cache read/write or invalidation occurs.
        install_settings_cache(None)
        with read_only_snapshot_session() as db:
            report = billing_numbering_readiness_report(db, query=query)
    except NumberingReadinessError as exc:
        print(
            json.dumps(
                {"complete": False, "admissible": False, "error": exc.code},
                sort_keys=True,
            )
        )
        return 2
    except SQLAlchemyError:
        print(
            json.dumps(
                {
                    "complete": False,
                    "admissible": False,
                    "error": "snapshot_unavailable",
                },
                sort_keys=True,
            )
        )
        return 2
    finally:
        install_settings_cache(previous_store)
    print(json.dumps(report.as_dict(), sort_keys=True, default=_json_scalar))
    return 2 if not report.complete else 0 if report.admissible else 1


if __name__ == "__main__":
    raise SystemExit(main())
