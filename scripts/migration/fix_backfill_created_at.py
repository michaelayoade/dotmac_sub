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


def run(which: str, batch: int, single: bool, id_batch: int = 0) -> None:
    t = TARGETS[which]
    conn = get_engine().connect().execution_options(isolation_level="AUTOCOMMIT")
    conn.execute(text("SET statement_timeout=0"))
    # Backfilled rows aren't touched by live writes, so waiting for a lock is
    # safe; disable the short default lock_timeout that would abort the update.
    conn.execute(text("SET lock_timeout=0"))

    if id_batch and which == "sessions":
        # Range-batch by splynx_session_id (its unique index makes each window an
        # index range scan, not a seq scan), committing per window. Combines
        # indexed access with incremental, resumable progress.
        lo, hi = conn.execute(
            text(
                "SELECT min(splynx_session_id), max(splynx_session_id) "
                "FROM radius_accounting_sessions WHERE splynx_session_id IS NOT NULL"
            )
        ).fetchone()
        sql = text(
            "UPDATE radius_accounting_sessions SET created_at = session_start "
            "WHERE splynx_session_id >= :lo AND splynx_session_id < :hi "
            "AND session_start IS NOT NULL AND created_at IS DISTINCT FROM session_start"
        )
        total, cur = 0, int(lo)
        while cur <= int(hi):
            nxt = cur + id_batch
            n = conn.execute(sql, {"lo": cur, "hi": nxt}).rowcount
            total += n
            print(f"  ids[{cur:,},{nxt:,}): +{n:,} (total {total:,})", flush=True)
            cur = nxt
        conn.close()
        print(f"DONE sessions (id-range): {total:,} rows corrected")
        return

    if single:
        # One pass: a single seq scan + update. Avoids the O(n^2) of repeated
        # LIMIT scans (each had to skip the growing set of already-fixed rows).
        sql = text(
            f"UPDATE {t['table']} SET created_at = {t['target']} "
            f"WHERE {t['filter']} AND created_at IS DISTINCT FROM {t['target']}"
        )
        n = conn.execute(sql).rowcount
        conn.close()
        print(f"DONE {which} (single): {n:,} rows corrected")
        return

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
    ap.add_argument(
        "--single",
        action="store_true",
        help="one UPDATE pass instead of LIMIT batches (faster for a full table)",
    )
    ap.add_argument(
        "--id-batch",
        type=int,
        default=0,
        help="sessions only: range-batch by splynx_session_id window of this size",
    )
    args = ap.parse_args()
    run(args.which, args.batch, args.single, args.id_batch)
