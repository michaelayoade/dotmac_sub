"""One-off: re-pull cleartext passwords from the Splynx API for services whose
Splynx row changed after the 2026-05-23 credential bootstrap, and repair any
stale radcheck/access_credentials entries.

Why: incremental sync does NOT carry per-service password changes. A customer
whose PPPoE password changed in Splynx after the bootstrap authed fine against
Splynx (old primary) but gets Access-Reject from dotmac (new primary).
Found via cust 100024016 (changed 2026-06-02, 60+ rejects post-cutover).

Usage (in dotmac_sub_app container):
    python -m scripts.migration.refresh_changed_passwords            # dry-run
    python -m scripts.migration.refresh_changed_passwords --execute
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys
from collections import defaultdict

import psycopg
from sqlalchemy import select

from app.db import SessionLocal
from app.models.catalog import AccessCredential
from app.services.credential_crypto import encrypt_credential
from scripts.migration.bootstrap_radius_from_splynx import _splynx_services
from scripts.migration.db_connections import splynx_connection

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Sliding window: cover anything changed since the last run with margin.
# Override with CHANGED_SINCE="YYYY-MM-DD HH:MM:SS" for a deep backfill.
def _default_changed_since() -> str:
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC) - timedelta(hours=26)).strftime("%Y-%m-%d %H:%M:%S")


CHANGED_SINCE = os.environ.get("CHANGED_SINCE") or _default_changed_since()


def main(execute: bool) -> None:
    radius_dsn = os.environ["RADIUS_DB_DSN"]

    # 1. candidate services changed in Splynx since the bootstrap
    with splynx_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT customer_id, login FROM services_internet "
                "WHERE deleted='0' AND status='active' AND login <> '' "
                "AND updated_at > %s",
                (CHANGED_SINCE,),
            )
            rows = cur.fetchall()
    by_customer: dict[int, list[str]] = defaultdict(list)
    for r in rows:
        cid = r[0] if not isinstance(r, dict) else r["customer_id"]
        login = r[1] if not isinstance(r, dict) else r["login"]
        by_customer[int(cid)].append(str(login).strip())
    logger.info(
        "%d services on %d customers changed since %s",
        len(rows), len(by_customer), CHANGED_SINCE,
    )

    # 2. current radcheck values (md5 only, in memory)
    rconn = psycopg.connect(radius_dsn)
    with rconn.cursor() as cur:
        cur.execute(
            "SELECT username, md5(value) FROM radcheck "
            "WHERE attribute='Cleartext-Password'"
        )
        radcheck_md5 = dict(cur.fetchall())

    stats = {"checked": 0, "api_failed": 0, "match": 0, "stale_fixed": 0,
             "missing_in_radcheck": 0, "no_api_password": 0}
    fixes: list[tuple[str, str]] = []  # (login, cleartext)

    for cid, logins in by_customer.items():
        svcs = _splynx_services(cid)
        if svcs is None:
            stats["api_failed"] += 1
            continue
        pw_map = {
            s["login"].strip(): s["password"].strip()
            for s in svcs
            if s.get("login") and s.get("password")
        }
        for login in logins:
            stats["checked"] += 1
            cleartext = pw_map.get(login)
            if not cleartext:
                stats["no_api_password"] += 1
                continue
            want = hashlib.md5(cleartext.encode()).hexdigest()  # noqa: S324
            have = radcheck_md5.get(login)
            if have is None:
                stats["missing_in_radcheck"] += 1
                fixes.append((login, cleartext))
            elif have != want:
                stats["stale_fixed"] += 1
                fixes.append((login, cleartext))
            else:
                stats["match"] += 1

    logger.info("scan done: %s", stats)
    for login, _ in fixes:
        logger.info("needs update: %s", login)

    if not execute:
        logger.info("DRY RUN — no writes")
        return

    # 3. apply: access_credentials (Fernet) + radcheck cleartext
    db = SessionLocal()
    try:
        for login, cleartext in fixes:
            cred = db.scalar(
                select(AccessCredential).where(AccessCredential.username == login)
            )
            if cred:
                cred.secret_hash = encrypt_credential(cleartext)
        db.commit()
    finally:
        db.close()

    with rconn.cursor() as cur:
        for login, cleartext in fixes:
            cur.execute("DELETE FROM radcheck WHERE username = %s", (login,))
            cur.execute(
                "INSERT INTO radcheck (username, attribute, op, value) "
                "VALUES (%s, 'Cleartext-Password', ':=', %s)",
                (login, cleartext),
            )
    rconn.commit()
    rconn.close()
    logger.info("applied %d password fixes", len(fixes))


if __name__ == "__main__":
    main(execute="--execute" in sys.argv)
