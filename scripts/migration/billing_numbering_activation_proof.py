#!/usr/bin/env python3
"""Print read-only invoice candidate evidence; every target field is required."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from datetime import date

from dotmac_kernel.settings_cache import active_settings_cache, install_settings_cache
from dotmac_numbering import SeriesConfiguration
from sqlalchemy.exc import SQLAlchemyError

from app.db import read_only_snapshot_session
from app.services.billing_numbering_activation_proof import (
    ActivationProofQuery,
    _scalar,
    billing_numbering_activation_proof_report,
)
from app.services.billing_numbering_readiness import NumberingReadinessError


def _boolean(value: str) -> bool:
    if value not in ("true", "false"):
        raise argparse.ArgumentTypeError("must be true or false")
    return value == "true"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-date", type=date.fromisoformat, required=True)
    for field in ("series-code", "prefix", "suffix", "separator", "reset-policy"):
        parser.add_argument(f"--{field}", required=True)
    for field in ("min-digits", "year-digits", "start-value"):
        parser.add_argument(f"--{field}", type=int, required=True)
    for field in ("include-year", "include-month"):
        parser.add_argument(f"--{field}", type=_boolean, required=True)
    args = parser.parse_args(argv)
    proposal = SeriesConfiguration(
        series_code=args.series_code,
        prefix=args.prefix,
        suffix=args.suffix,
        separator=args.separator,
        min_digits=args.min_digits,
        include_year=args.include_year,
        year_digits=args.year_digits,
        include_month=args.include_month,
        reset_policy=args.reset_policy,
        start_value=args.start_value,
    )
    query = ActivationProofQuery(reference_date=args.reference_date, proposal=proposal)
    previous = active_settings_cache()
    try:
        install_settings_cache(None)
        with read_only_snapshot_session() as db:
            report = billing_numbering_activation_proof_report(db, query=query)
    except (NumberingReadinessError, SQLAlchemyError) as exc:
        print(
            json.dumps(
                {
                    "complete": False,
                    "candidate_proven": False,
                    "activatable": False,
                    "error": exc.code
                    if isinstance(exc, NumberingReadinessError)
                    else "snapshot_unavailable",
                },
                sort_keys=True,
            )
        )
        return 2
    finally:
        install_settings_cache(previous)
    print(json.dumps(report.as_dict(), sort_keys=True, default=_scalar))
    return 2 if not report.complete else 0 if report.candidate_proven else 1


if __name__ == "__main__":
    raise SystemExit(main())
