"""Import FUP policies/rules from Splynx fup_policies into FupPolicy/FupRule.

DRY-RUN by default.

    python -m scripts.migration.import_fup_policies_from_splynx            # dry-run
    python -m scripts.migration.import_fup_policies_from_splynx --execute  # apply

Requires the Splynx DB env + DATABASE_URL.
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
from app.services.migrations.sync_fup_policies_from_splynx import import_fup_policies

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("import_fup_policies_from_splynx")


def run(*, execute: bool) -> None:
    with splynx_connection() as conn:
        rows = list(fetch_all(conn, "SELECT * FROM fup_policies"))
    logger.info("fetched %d fup_policies rows from Splynx", len(rows))
    with dotmac_session() as db:
        summary = import_fup_policies(db, rows, commit=execute)
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
