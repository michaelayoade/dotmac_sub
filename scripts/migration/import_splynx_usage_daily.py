"""Backfill Splynx daily traffic (``traffic_counter``) into
``subscriber_daily_usage``.

DRY-RUN by default. Idempotent via the (splynx_service_id, usage_date) unique
key + ON CONFLICT DO NOTHING. Rows whose service has no subscription mapping
are still imported (subscription_id NULL) so no history is lost; they can be
re-attributed later. History runs back to 2018.

    python -m scripts.migration.import_splynx_usage_daily            # dry-run
    python -m scripts.migration.import_splynx_usage_daily --execute  # apply
"""

from __future__ import annotations

import argparse
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db import SessionLocal, get_engine
from app.models.usage import SubscriberDailyUsage
from scripts.migration._splynx_conn import splynx_connection

BATCH = 10000
LAGOS = ZoneInfo("Africa/Lagos")


def _load_service_map(db) -> dict[int, uuid.UUID]:
    rows = db.execute(
        text(
            "SELECT splynx_id, dotmac_id FROM splynx_id_mappings "
            "WHERE entity_type='service'"
        )
    ).fetchall()
    return {int(r[0]): r[1] for r in rows}


def run(*, execute: bool) -> None:
    db = SessionLocal()
    service_map = _load_service_map(db)
    print(f"loaded {len(service_map)} service->subscription mappings")

    stats = {"scanned": 0, "mapped": 0, "unmapped": 0}
    batch: list[dict] = []
    # AUTOCOMMIT: each batch commits on execute, so there is never an open
    # transaction between MySQL fetches (avoids idle-in-transaction timeout).
    conn = get_engine().connect().execution_options(isolation_level="AUTOCOMMIT")
    conn.execute(text("SET statement_timeout=0"))

    def flush() -> None:
        if not batch:
            return
        if execute:
            stmt = pg_insert(SubscriberDailyUsage.__table__).on_conflict_do_nothing(
                index_elements=["splynx_service_id", "usage_date"],
            )
            conn.execute(stmt, batch)
        batch.clear()

    with splynx_connection(streaming=True) as myconn:
        cur = myconn.cursor()
        cur.execute("SELECT service_id, date, up, down FROM traffic_counter")
        for row in cur:
            stats["scanned"] += 1
            sub_id = service_map.get(int(row["service_id"]))
            stats["mapped" if sub_id else "unmapped"] += 1
            batch.append(
                {
                    "id": uuid.uuid4(),
                    "subscription_id": sub_id,
                    "splynx_service_id": int(row["service_id"]),
                    "usage_date": row["date"],
                    "upload_bytes": int(row["up"] or 0),
                    "download_bytes": int(row["down"] or 0),
                    "source": "splynx_traffic_counter",
                    # Backfilled rows: created_at reflects the usage day itself
                    # (midnight, subscriber-region tz), not import time.
                    "created_at": datetime(
                        row["date"].year,
                        row["date"].month,
                        row["date"].day,
                        tzinfo=LAGOS,
                    ),
                }
            )
            if len(batch) >= BATCH:
                flush()
                if stats["scanned"] % 500000 == 0:
                    print(f"  ...scanned {stats['scanned']:,}")
        flush()

    conn.close()
    db.close()

    print("\n=== SUMMARY ===")
    for k, v in stats.items():
        print(f"  {k}: {v:,}")
    if not execute:
        print("\n(DRY-RUN — nothing written. Re-run with --execute to apply.)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()
    run(execute=args.execute)
