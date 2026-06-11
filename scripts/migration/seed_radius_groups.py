"""Seed the dotmac-* RADIUS group definitions (phase 1, revised 2026-06-11).

Writes radgroupcheck/radgroupreply rows for the three groups the access-state
model uses. Captive carries the DEPLOYED walled-garden attrs (real IP +
Mikrotik-Address-List=suspended + standing filter rules), not the original
Framed-Pool design — see docs/radius_state_refactor/phase0_state_model.md.

Idempotent: deletes the dotmac-* rows it owns, then re-inserts. Run BEFORE
the phase-5 backfill so group membership has something to resolve against.

Usage:
  docker exec -e PYTHONPATH=/app -w /app dotmac_sub_app \\
      python -m scripts.migration.seed_radius_groups --execute
"""

from __future__ import annotations

import logging
import os
import sys

import psycopg

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

GROUPCHECK_ROWS = [
    # Hard tier — abuse/fraud only (NOT default dunning).
    ("dotmac-suspended", "Auth-Type", ":=", "Reject"),
]

GROUPREPLY_ROWS = [
    ("dotmac-active", "Service-Type", ":=", "Framed-User"),
    ("dotmac-active", "Framed-Protocol", ":=", "PPP"),
    # Captive = deployed walled-garden mechanism: real IP, throttled,
    # address-list jump allows only the portal.
    ("dotmac-captive", "Service-Type", ":=", "Framed-User"),
    ("dotmac-captive", "Framed-Protocol", ":=", "PPP"),
    ("dotmac-captive", "Mikrotik-Rate-Limit", ":=", "1M/1M"),
    ("dotmac-captive", "Mikrotik-Address-List", ":=", "suspended"),
]


def seed(dry_run: bool = True) -> dict[str, int]:
    radius_dsn = os.environ.get("RADIUS_DB_DSN", "")
    if not radius_dsn:
        raise RuntimeError("RADIUS_DB_DSN not set")

    stats = {
        "radgroupcheck_rows": len(GROUPCHECK_ROWS),
        "radgroupreply_rows": len(GROUPREPLY_ROWS),
    }
    if dry_run:
        logger.info("DRY RUN — would write: %s", stats)
        for row in GROUPCHECK_ROWS:
            logger.info("  radgroupcheck: %s", row)
        for row in GROUPREPLY_ROWS:
            logger.info("  radgroupreply: %s", row)
        return stats

    conn = psycopg.connect(radius_dsn)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM radgroupcheck WHERE groupname LIKE 'dotmac-%'")
            cur.executemany(
                "INSERT INTO radgroupcheck (groupname, attribute, op, value) "
                "VALUES (%s, %s, %s, %s)",
                GROUPCHECK_ROWS,
            )
            cur.execute("DELETE FROM radgroupreply WHERE groupname LIKE 'dotmac-%'")
            cur.executemany(
                "INSERT INTO radgroupreply (groupname, attribute, op, value) "
                "VALUES (%s, %s, %s, %s)",
                GROUPREPLY_ROWS,
            )
        conn.commit()
        logger.info("committed group definitions: %s", stats)
    finally:
        conn.close()
    return stats


if __name__ == "__main__":
    if "--execute" in sys.argv:
        seed(dry_run=False)
    else:
        seed(dry_run=True)
        print("\nTo execute: python -m scripts.migration.seed_radius_groups --execute")
