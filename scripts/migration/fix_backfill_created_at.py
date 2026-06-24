"""Set created_at on Splynx-backfilled usage rows to the ORIGINAL event date
(not import time), in resumable autocommit batches.

- subscriber_daily_usage: created_at <- usage_date @ midnight Africa/Lagos
- radius_accounting_sessions (backfilled only): created_at <- session_start

Each batch updates up to --batch rows whose created_at is still wrong, then
commits, looping until none remain. Safe to re-run; safe alongside live writes
(only touches splynx-tagged rows).

    python -m scripts.migration.fix_backfill_created_at daily
    python -m scripts.migration.fix_backfill_created_at sessions
"""

from __future__ import annotations

import argparse

from sqlalchemy import text

from app.db import get_engine

BATCH = 100000

TARGETS = {
    "daily": {
        "table": "subscriber_daily_usage",
        "target": "(usage_date::timestamp AT TIME ZONE 'Africa/Lagos')",
        "filter": "source = 'splynx_traffic_counter'",
    },
    "sessions": {
        "table": "radius_accounting_sessions",
        "target": "session_start",
        "filter": "splynx_session_id IS NOT NULL AND session_start IS NOT NULL",
    },
}


def run(which: str, batch: int) -> None:
    t = TARGETS[which]
    sql = text(
        f"""
        UPDATE {t["table"]} SET created_at = {t["target"]}
        WHERE ctid IN (
            SELECT ctid FROM {t["table"]}
            WHERE {t["filter"]} AND created_at IS DISTINCT FROM {t["target"]}
            LIMIT :batch
        )
        """
    )
    conn = get_engine().connect().execution_options(isolation_level="AUTOCOMMIT")
    conn.execute(text("SET statement_timeout=0"))
    # Backfilled rows aren't touched by live writes, so waiting for a lock is
    # safe; disable the short default lock_timeout that would abort a batch.
    conn.execute(text("SET lock_timeout=0"))
    total = 0
    while True:
        n = conn.execute(sql, {"batch": batch}).rowcount
        total += n
        print(f"  {which}: +{n:,} (total {total:,})", flush=True)
        if n == 0:
            break
    conn.close()
    print(f"DONE {which}: {total:,} rows corrected")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("which", choices=list(TARGETS))
    ap.add_argument("--batch", type=int, default=BATCH)
    args = ap.parse_args()
    run(args.which, args.batch)
