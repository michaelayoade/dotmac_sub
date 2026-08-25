#!/usr/bin/env python3
"""Report postpaid eligibility parity against the composed Collections seam.

The report is aggregate, PII-free, and read-only. It does not create module
cases or move authority. By default mismatches fail the command as an
operational mismatch check. ``--as-of`` and ``--observe-at`` make one
time-dependent comparison reproducible while retaining the same read-only
snapshot. The exit status is not sealed cutover evidence: the output carries
no cohort identity, source revision, or evidence digest. Use ``--report-only``
when mismatches should not change the exit status.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from datetime import UTC, datetime

from app.db import read_only_snapshot_session
from app.services.collections_module_shadow import postpaid_eligibility_parity_report


def _aware_instant(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("must be timezone-aware")
    return parsed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="return zero even when eligibility mismatches remain",
    )
    parser.add_argument(
        "--as-of",
        type=_aware_instant,
        help="aware evaluation instant; defaults to the current instant",
    )
    parser.add_argument(
        "--observe-at",
        type=_aware_instant,
        help=(
            "aware temporal observation instant at or after --as-of; "
            "defaults to the evaluation instant"
        ),
    )
    args = parser.parse_args(argv)
    evaluation_instant = args.as_of or datetime.now(UTC)
    observation_instant = args.observe_at or evaluation_instant
    if observation_instant < evaluation_instant:
        parser.error("--observe-at must not be earlier than --as-of")

    with read_only_snapshot_session() as db:
        report = postpaid_eligibility_parity_report(
            db,
            as_of=evaluation_instant,
            observe_at=observation_instant,
        )

    print(json.dumps(report.as_dict(), sort_keys=True))
    if not report.is_parity_safe and not args.report_only:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
