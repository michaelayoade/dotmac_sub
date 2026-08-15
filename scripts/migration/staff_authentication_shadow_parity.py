#!/usr/bin/env python3
"""Report whether staff authentication can be resolved through Party identically.

Shadow evidence for the staff Party read cutover. Read-only, aggregate and
PII-free: the output carries counts and stable reason codes only, so it can be
run against a production-derived restore and pasted into a review unredacted.

Exits non-zero while any blocking cohort remains, so it can gate the cutover
directly. `--report-only` surveys without gating.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from app.db import SessionLocal
from app.services.staff_authentication_shadow import (
    staff_authentication_parity_report,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="return zero even while blocking cohorts remain",
    )
    args = parser.parse_args(argv)

    with SessionLocal() as db:
        # A parity claim assembled from several statements has to see one
        # snapshot, or a login landing mid-run can make the cohorts disagree
        # with each other and with themselves. Read-only is the second half:
        # this report must be incapable of writing to the database it measures.
        #
        # Both are requested as connection execution options rather than issued
        # as `SET TRANSACTION`. That statement is only legal as the FIRST one in
        # a transaction, and it never is here: the operator-tenant hook installs
        # `app.current_tenant` via set_config on `after_begin`, so a statement
        # has already run by the time any caller-issued SQL arrives. The
        # previous form raised ActiveSqlTransaction on every PostgreSQL run,
        # which SQLite unit coverage could not see because it skipped the branch
        # entirely.
        if db.bind is not None and db.bind.dialect.name == "postgresql":
            db.connection(
                execution_options={
                    "isolation_level": "REPEATABLE READ",
                    "postgresql_readonly": True,
                }
            )
        report = staff_authentication_parity_report(db)
        db.rollback()

    print(json.dumps(report.as_dict(), sort_keys=True))
    if not report.is_read_cutover_safe and not args.report_only:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
