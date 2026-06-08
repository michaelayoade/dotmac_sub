"""Import add-ons (one-time fees + public-IP blocks) from Splynx into the catalog.

Reads ``tariffs_one_time`` and the ``/NN IP`` entries of ``tariffs_custom`` and
upserts them as catalog add-ons (idempotent on ``splynx_source``). Additive —
nothing existing is modified.

Usage::

    python -m scripts.migration.import_addons_from_splynx          # dry-run
    python -m scripts.migration.import_addons_from_splynx --execute

Requires the Splynx DB env (SPLYNX_MYSQL_*) and DATABASE_URL.
"""

from __future__ import annotations

import argparse
import logging

from app.services.migrations.db_connections import (
    dotmac_session,
    fetch_all,
    splynx_connection,
)
from app.services.migrations.sync_addons_from_splynx import import_addon_rows

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("import_addons_from_splynx")


def run(*, execute: bool) -> None:
    with splynx_connection() as conn:
        one_time = list(fetch_all(conn, "SELECT * FROM tariffs_one_time"))
        custom = list(fetch_all(conn, "SELECT * FROM tariffs_custom"))
    logger.info(
        "fetched %d one-time + %d custom tariffs from Splynx",
        len(one_time),
        len(custom),
    )

    with dotmac_session() as db:
        summary = import_addon_rows(db, one_time, custom, commit=execute)
        if not execute:
            db.rollback()
            logger.info("DRY-RUN (no commit) — would import: %s", summary)
        else:
            logger.info("Imported: %s", summary)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--execute",
        action="store_true",
        help="Commit the import (default is a dry-run that rolls back).",
    )
    args = p.parse_args()
    run(execute=args.execute)
