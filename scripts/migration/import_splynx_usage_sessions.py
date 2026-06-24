"""Backfill Splynx per-session accounting (``statistics``) into
``radius_accounting_sessions``.

DRY-RUN by default — reports coverage and writes nothing. Re-run with
``--execute`` to apply. Idempotent: each row carries ``splynx_session_id``
(= Splynx ``statistics.id``) and inserts use ON CONFLICT DO NOTHING against the
unique partial index, so re-runs only fill gaps.

To avoid double-counting lifetime totals against the live RADIUS feed (which
owns sessions from the cutover onward), only rows with ``start_date`` strictly
before ``--cutover-date`` (default 2026-06-02) are imported.

    python -m scripts.migration.import_splynx_usage_sessions            # dry-run
    python -m scripts.migration.import_splynx_usage_sessions --execute  # apply
"""

from __future__ import annotations

import argparse
import socket
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db import SessionLocal, get_engine
from app.models.usage import RadiusAccountingSession
from scripts.migration._splynx_conn import splynx_connection

LAGOS = ZoneInfo("Africa/Lagos")
DEFAULT_CUTOVER = "2026-06-02"
BATCH = 5000


def _to_dt(d, t) -> datetime | None:
    """Combine a MySQL DATE + TIME (pymysql returns TIME as timedelta)."""
    if d is None:
        return None
    base = datetime(d.year, d.month, d.day, tzinfo=LAGOS)
    if t is not None:
        base = base + t  # t is a datetime.timedelta
    return base


def _ipv4(raw) -> str | None:
    if not raw or len(raw) != 4 or raw == b"\x00\x00\x00\x00":
        return None
    try:
        return socket.inet_ntoa(raw)
    except OSError:
        return None


def _load_service_map(db) -> dict[int, uuid.UUID]:
    rows = db.execute(
        text(
            "SELECT splynx_id, dotmac_id FROM splynx_id_mappings "
            "WHERE entity_type='service'"
        )
    ).fetchall()
    return {int(r[0]): r[1] for r in rows}


def _load_login_map(db) -> dict[str, tuple[uuid.UUID, uuid.UUID]]:
    rows = db.execute(
        text("SELECT username, id, subscriber_id FROM access_credentials")
    ).fetchall()
    return {str(r[0]): (r[1], r[2]) for r in rows if r[0] is not None}


def run(
    *,
    execute: bool,
    cutover: str,
    include_errors: bool,
    min_id: int = 0,
    min_start_date: str = "",
) -> None:
    db = SessionLocal()
    service_map = _load_service_map(db)
    login_map = _load_login_map(db)
    print(
        f"loaded {len(service_map)} service->subscription mappings, "
        f"{len(login_map)} login->credential mappings"
    )

    stats = {
        "scanned": 0,
        "inserted_or_seen": 0,
        "skipped_no_subscription": 0,
        "skipped_error_session": 0,
        "matched_credential": 0,
    }
    unmapped_services: set[int] = set()

    where = "start_date < %s"
    params = [cutover]
    if not include_errors:
        where += " AND error = 0"
    if min_start_date:
        # Resume by date: MySQL scans this query in start_date order (the range
        # predicate uses the start_date index), so a dropped run leaves the
        # latest-dated tail. Re-running from the last fully-imported date picks
        # up only that tail (ON CONFLICT de-dupes the boundary day).
        where += " AND start_date >= %s"
        params.append(min_start_date)
    if min_id:
        where += " AND id >= %s"
        params.append(min_id)

    batch: list[dict] = []
    # AUTOCOMMIT: each batch commits on execute, so there is never an open
    # transaction between MySQL fetches (avoids idle-in-transaction timeout).
    conn = get_engine().connect().execution_options(isolation_level="AUTOCOMMIT")
    conn.execute(text("SET statement_timeout=0"))

    def flush() -> None:
        if not batch:
            return
        if execute:
            stmt = pg_insert(RadiusAccountingSession.__table__).on_conflict_do_nothing(
                index_elements=["splynx_session_id"],
                index_where=text("splynx_session_id IS NOT NULL"),
            )
            conn.execute(stmt, batch)
        batch.clear()

    with splynx_connection(streaming=True) as myconn:
        cur = myconn.cursor()
        cur.execute(
            f"SELECT id, service_id, login, in_bytes, out_bytes, start_date, "
            f"start_time, end_date, end_time, ipv4, session_id, terminate_cause "
            f"FROM statistics WHERE {where}",
            params,
        )
        for row in cur:
            stats["scanned"] += 1
            sub_id = service_map.get(int(row["service_id"]))
            if sub_id is None:
                stats["skipped_no_subscription"] += 1
                unmapped_services.add(int(row["service_id"]))
                continue
            cred = login_map.get(str(row["login"])) if row["login"] else None
            if cred:
                stats["matched_credential"] += 1
            sess_id = row["session_id"] or str(row["id"])
            sess_start = _to_dt(row["start_date"], row["start_time"])
            batch.append(
                {
                    "id": uuid.uuid4(),
                    "subscription_id": sub_id,
                    "access_credential_id": cred[0] if cred else None,
                    "session_id": str(sess_id)[:120],
                    "status_type": "stop",
                    "session_start": sess_start,
                    "session_end": _to_dt(row["end_date"], row["end_time"]),
                    # Backfilled rows: created_at reflects the original session
                    # time, not import time, so history reads at its true date.
                    "created_at": sess_start,
                    "input_octets": int(row["in_bytes"] or 0),
                    "output_octets": int(row["out_bytes"] or 0),
                    "terminate_cause": (
                        str(row["terminate_cause"])
                        if row["terminate_cause"] is not None
                        else None
                    ),
                    "framed_ip_address": _ipv4(row["ipv4"]),
                    "splynx_session_id": int(row["id"]),
                }
            )
            stats["inserted_or_seen"] += 1
            if len(batch) >= BATCH:
                flush()
                if stats["scanned"] % 100000 == 0:
                    print(f"  ...scanned {stats['scanned']:,}")
        flush()

    conn.close()
    db.close()

    print("\n=== SUMMARY ===")
    for k, v in stats.items():
        print(f"  {k}: {v:,}")
    print(f"  distinct unmapped service_ids: {len(unmapped_services):,}")
    if not execute:
        print("\n(DRY-RUN — nothing written. Re-run with --execute to apply.)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--cutover-date", default=DEFAULT_CUTOVER)
    ap.add_argument(
        "--include-errors",
        action="store_true",
        help="also import failed/errored auth rows (error<>0)",
    )
    ap.add_argument(
        "--min-id",
        type=int,
        default=0,
        help="resume: only scan statistics.id >= this (idempotent regardless)",
    )
    ap.add_argument(
        "--min-start-date",
        default="",
        help="resume: only scan rows with start_date >= this YYYY-MM-DD",
    )
    args = ap.parse_args()
    run(
        execute=args.execute,
        cutover=args.cutover_date,
        include_errors=args.include_errors,
        min_id=args.min_id,
        min_start_date=args.min_start_date,
    )
