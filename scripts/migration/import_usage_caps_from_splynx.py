"""Import per-plan data caps from Splynx (fup_limits) into UsageAllowances.

DRY-RUN by default — prints what would be capped, commits nothing.

    python -m scripts.migration.import_usage_caps_from_splynx            # dry-run
    python -m scripts.migration.import_usage_caps_from_splynx --execute  # apply

Capping a plan activates the metering + FUP pipeline for it. Requires the Splynx
DB env + DATABASE_URL.
"""

from __future__ import annotations

import argparse
import json
import logging

from app.services.migrations.db_connections import (
    dotmac_session,
    fetch_all,
    splynx_connection,
)
from app.services.migrations.sync_usage_caps_from_splynx import import_usage_caps

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("import_usage_caps_from_splynx")


def run(*, execute: bool) -> None:
    with splynx_connection() as conn:
        rows = list(fetch_all(conn, "SELECT * FROM fup_limits"))
    logger.info("fetched %d fup_limits rows from Splynx", len(rows))
    with dotmac_session() as db:
        summary = import_usage_caps(db, rows, commit=execute)
        print(json.dumps(summary, indent=2, default=str))
        if not execute:
            db.rollback()
            print("(DRY-RUN — nothing committed. Re-run with --execute to apply.)")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--execute",
        action="store_true",
        help="Commit (default is a dry-run that rolls back).",
    )
    args = p.parse_args()
    run(execute=args.execute)
