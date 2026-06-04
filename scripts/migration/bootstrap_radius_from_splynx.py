"""One-pass bootstrap: Splynx API → access_credentials (encrypted) + radcheck/radreply.

Replaces the two-stage radius_sync.py flow with a single coherent pass:
  per active/blocked subscription with a login:
    1. Pull cleartext password from Splynx API (ONLY API hop we need)
    2. Fernet-encrypt + UPSERT access_credentials.secret_hash
    3. UPSERT radcheck (cleartext) + radreply (full attrs from dotmac_sub joins)

After this runs once, going forward all radius updates should come from
scripts/migration/populate_radius_from_subs.py (or its Celery handler) —
no more Splynx API dependency.

Batches API calls by Splynx customer (one call returns all their services).

Usage:
    # dry-run (no DB writes, no API calls until --execute):
    docker exec -e PYTHONPATH=/app -w /app dotmac_sub_app \\
        python -m scripts.migration.bootstrap_radius_from_splynx --limit 10
    # live, small batch:
    docker exec -e PYTHONPATH=/app -w /app dotmac_sub_app \\
        python -m scripts.migration.bootstrap_radius_from_splynx --execute --limit 10
    # live, full population:
    docker exec -e PYTHONPATH=/app -w /app dotmac_sub_app \\
        python -m scripts.migration.bootstrap_radius_from_splynx --execute
"""

from __future__ import annotations

import argparse
import logging
import os
import time
import uuid
from collections import defaultdict
from datetime import UTC, datetime

import psycopg
import requests
import urllib3
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.db import SessionLocal
from app.models.catalog import (
    AccessCredential,
    CatalogOffer,
    RadiusProfile,
    Subscription,
    SubscriptionStatus,
)
from app.models.subscriber import Subscriber
from app.services.credential_crypto import encrypt_credential

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

ACCT_INTERIM_SECONDS = 300
SUSPENDED_ADDRESS_LIST = "suspended"

SPLYNX_API_BASE = os.environ.get("SPLYNX_API_BASE", "")
SPLYNX_API_KEY = os.environ.get("SPLYNX_API_KEY", "")
SPLYNX_API_SECRET = os.environ.get("SPLYNX_API_SECRET", "")
SPLYNX_HOST_HEADER = os.environ.get("SPLYNX_HOST_HEADER", "")
SPLYNX_VERIFY_TLS = os.environ.get("SPLYNX_VERIFY_TLS", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
if not SPLYNX_VERIFY_TLS:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _splynx_customer(customer_id: int) -> dict | None:
    """Fetch a customer record (incl. customer-level cleartext password)."""
    if not (SPLYNX_API_BASE and SPLYNX_API_KEY and SPLYNX_API_SECRET):
        return None
    url = f"{SPLYNX_API_BASE}/api/2.0/admin/customers/customer/{customer_id}"
    headers = {"Content-Type": "application/json"}
    if SPLYNX_HOST_HEADER:
        headers["Host"] = SPLYNX_HOST_HEADER
    try:
        r = requests.get(
            url, auth=(SPLYNX_API_KEY, SPLYNX_API_SECRET),
            headers=headers, verify=SPLYNX_VERIFY_TLS, timeout=20,
        )
        if r.status_code == 200:
            return r.json() if isinstance(r.json(), dict) else None
    except requests.RequestException:
        pass
    return None


def _splynx_services(customer_id: int) -> list[dict] | None:
    """Fetch the internet-services list for a Splynx customer. None on failure."""
    if not (SPLYNX_API_BASE and SPLYNX_API_KEY and SPLYNX_API_SECRET):
        raise RuntimeError(
            "SPLYNX_API_BASE/KEY/SECRET must be set for the API bootstrap"
        )
    url = f"{SPLYNX_API_BASE}/api/2.0/admin/customers/customer/{customer_id}/internet-services"
    headers = {"Content-Type": "application/json"}
    if SPLYNX_HOST_HEADER:
        headers["Host"] = SPLYNX_HOST_HEADER
    try:
        r = requests.get(
            url,
            auth=(SPLYNX_API_KEY, SPLYNX_API_SECRET),
            headers=headers,
            verify=SPLYNX_VERIFY_TLS,
            timeout=20,
        )
    except requests.RequestException as exc:
        logger.warning("API exception for customer %d: %s", customer_id, exc)
        return None
    if r.status_code == 200:
        return r.json() if isinstance(r.json(), list) else None
    if r.status_code == 404:
        return []
    logger.warning("API customer %d returned %d", customer_id, r.status_code)
    return None


def _rate_limit(offer: CatalogOffer | None, profile: RadiusProfile | None) -> str | None:
    if profile and profile.mikrotik_rate_limit:
        return profile.mikrotik_rate_limit
    if offer and offer.speed_download_mbps and offer.speed_upload_mbps:
        return f"{offer.speed_download_mbps}M/{offer.speed_upload_mbps}M"
    return None


def _radreply_attrs(
    sub: Subscription, offer: CatalogOffer | None, profile: RadiusProfile | None
) -> list[tuple[str, str, str]]:
    attrs: list[tuple[str, str, str]] = [
        ("Service-Type", ":=", "Framed-User"),
        ("Framed-Protocol", ":=", "PPP"),
        ("Acct-Interim-Interval", ":=", str(ACCT_INTERIM_SECONDS)),
    ]
    if sub.ipv4_address:
        attrs.append(("Framed-IP-Address", ":=", sub.ipv4_address))
    rate = _rate_limit(offer, profile)
    if rate:
        attrs.append(("Mikrotik-Rate-Limit", ":=", rate))
    sim = (profile.simultaneous_use if profile else None) or 1
    attrs.append(("Simultaneous-Use", ":=", str(sim)))
    if profile and profile.idle_timeout:
        attrs.append(("Idle-Timeout", ":=", str(profile.idle_timeout)))
    if sub.status == SubscriptionStatus.blocked:
        attrs.append(("Mikrotik-Address-List", ":=", SUSPENDED_ADDRESS_LIST))
    return attrs


def _upsert_access_credential(
    db, subscriber_id: uuid.UUID, username: str, cleartext: str,
    radius_profile_id: uuid.UUID | None,
) -> None:
    """Create or update the AccessCredential row, Fernet-encrypting the password."""
    cred = db.scalar(
        select(AccessCredential).where(AccessCredential.username == username)
    )
    encrypted = encrypt_credential(cleartext)
    if cred is None:
        cred = AccessCredential(
            id=uuid.uuid4(),
            subscriber_id=subscriber_id,
            username=username,
            secret_hash=encrypted,
            is_active=True,
            radius_profile_id=radius_profile_id,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        db.add(cred)
    else:
        cred.secret_hash = encrypted
        cred.is_active = True
        if radius_profile_id and not cred.radius_profile_id:
            cred.radius_profile_id = radius_profile_id
        cred.updated_at = datetime.now(UTC)


def _upsert_radius_rows(
    cur, sub: Subscription, cleartext: str,
    offer: CatalogOffer | None, profile: RadiusProfile | None,
) -> int:
    """Write radcheck + radreply rows. Returns count of radreply attrs written."""
    cur.execute("DELETE FROM radcheck WHERE username = %s", (sub.login,))
    cur.execute(
        "INSERT INTO radcheck (username, attribute, op, value) "
        "VALUES (%s, 'Cleartext-Password', ':=', %s)",
        (sub.login, cleartext),
    )
    attrs = _radreply_attrs(sub, offer, profile)
    cur.execute("DELETE FROM radreply WHERE username = %s", (sub.login,))
    for attr, op, val in attrs:
        cur.execute(
            "INSERT INTO radreply (username, attribute, op, value) "
            "VALUES (%s, %s, %s, %s)",
            (sub.login, attr, op, val),
        )
    return len(attrs)


def run(
    dry_run: bool = True,
    limit: int | None = None,
    missing_only: bool = False,
    customer_ids: set[int] | None = None,
    prefer_customer_password: bool = False,
) -> dict[str, int]:
    radius_dsn = os.environ.get("RADIUS_DB_DSN", "")
    if not radius_dsn:
        raise RuntimeError("RADIUS_DB_DSN not set")

    stats = defaultdict(int)
    db = SessionLocal()
    try:
        # Pull all active+blocked subs with login, plus their offer/profile
        q = (
            select(Subscription)
            .options(
                joinedload(Subscription.offer),
                joinedload(Subscription.radius_profile),
            )
            .where(
                Subscription.status.in_(
                    [SubscriptionStatus.active, SubscriptionStatus.blocked]
                ),
                Subscription.login.isnot(None),
            )
        )
        if limit:
            q = q.limit(limit)
        subs = db.execute(q).unique().scalars().all()
        stats["subs_considered"] = len(subs)

        if missing_only:
            # Filter to subs whose login has no usable AccessCredential
            # (either missing entirely OR present but decrypt fails).
            from app.models.catalog import AccessCredential
            from app.services.credential_crypto import decrypt_credential

            usable_users: set[str] = set()
            for username, secret_hash in db.execute(
                select(AccessCredential.username, AccessCredential.secret_hash).where(
                    AccessCredential.is_active.is_(True),
                    AccessCredential.secret_hash.isnot(None),
                )
            ).all():
                try:
                    pw = decrypt_credential(secret_hash)
                    # >30-char decrypted values are the phase1 bogus blobs (raw
                    # encrypted-at-rest MySQL value, not actual cleartext).
                    # Treat as not-usable so they get refreshed from Splynx API.
                    if pw and len(pw) <= 30:
                        usable_users.add(username)
                except Exception:
                    pass  # bad encryption — treat as not-usable
            before = len(subs)
            subs = [s for s in subs if s.login not in usable_users]
            stats["subs_already_usable_filtered_out"] = before - len(subs)
            logger.info(
                "missing-only mode: filtered %d → %d (skip %d that already have decryptable AccessCredential)",
                before,
                len(subs),
                before - len(subs),
            )
        logger.info("considering %d subscriptions", len(subs))

        # Pre-fetch subscribers (need splynx_customer_id) — batch lookup
        sub_ids = {s.subscriber_id for s in subs}
        subscribers = {
            s.id: s
            for s in db.scalars(
                select(Subscriber).where(Subscriber.id.in_(sub_ids))
            ).all()
        }

        # Group subscriptions by Splynx customer ID for batched API calls
        subs_by_splynx_cid: dict[int, list[Subscription]] = defaultdict(list)
        for sub in subs:
            subr = subscribers.get(sub.subscriber_id)
            if subr is None or subr.splynx_customer_id is None:
                stats["skipped_no_splynx_cid"] += 1
                continue
            subs_by_splynx_cid[subr.splynx_customer_id].append(sub)

        # Targeted filter — only process specific Splynx customer IDs (for testing).
        if customer_ids:
            before = len(subs_by_splynx_cid)
            subs_by_splynx_cid = {
                cid: lst for cid, lst in subs_by_splynx_cid.items() if cid in customer_ids
            }
            stats["customer_ids_filter_kept"] = len(subs_by_splynx_cid)
            stats["customer_ids_filter_dropped"] = before - len(subs_by_splynx_cid)
            logger.info(
                "--customer-ids filter: %d → %d customers (requested %s)",
                before, len(subs_by_splynx_cid), sorted(customer_ids),
            )

        logger.info(
            "%d unique Splynx customers to query (will issue 1 API call each)",
            len(subs_by_splynx_cid),
        )

        if dry_run:
            # Don't open the radius-db connection or call the API in dry-run.
            # Just project counts.
            stats["would_call_api"] = len(subs_by_splynx_cid)
            stats["would_upsert"] = sum(len(v) for v in subs_by_splynx_cid.values())
            logger.info("DRY RUN — pre-counts only; pass --execute to do the work")
            return dict(stats)

        rconn = psycopg.connect(radius_dsn)
        rconn.autocommit = False
        try:
            processed_customers = 0
            for splynx_cid, sub_list in sorted(subs_by_splynx_cid.items()):
                services = _splynx_services(splynx_cid)
                if services is None:
                    stats["api_failures"] += 1
                    processed_customers += 1
                    continue

                # login → cleartext password map for this customer.
                # Splynx auth model: services_internet.password is per-service;
                # when empty, Splynx_radd falls back to customers.password. We
                # mirror that fallback so logins without a service password
                # still get a usable credential.
                #
                # 2026-05-25: Live tcpdump on Gwarimpa/SPDC/AFR proved Splynx_radd
                # ALSO authenticates against customers.password when service-level
                # diverges (cust 9833: svc=w7EGXA0g vs cust=ZTB8FzNv; cust 15862:
                # svc=9VtHj2W truncated vs cust=9VtHj2Wa). The --prefer-customer-password
                # flag inverts the priority: always use customer-level when present,
                # service-level as fallback. Needed for MS-CHAPv2 parity.
                pw_map = {
                    svc["login"].strip(): svc["password"].strip()
                    for svc in services
                    if svc.get("login") and svc.get("password")
                }
                cust_pw = ""
                if prefer_customer_password:
                    cust = _splynx_customer(splynx_cid)
                    cust_pw = (cust or {}).get("password", "").strip()
                    if cust_pw:
                        # Override every login's password with customer-level.
                        for svc in services:
                            login = (svc.get("login") or "").strip()
                            if login:
                                pw_map[login] = cust_pw
                        stats["prefer_customer_password_applied"] += 1
                # Fill in any login that's missing a password from the customer-level
                logins_needing_fallback = [
                    svc["login"].strip() for svc in services
                    if svc.get("login") and not svc.get("password")
                ]
                if logins_needing_fallback and not cust_pw:
                    cust = _splynx_customer(splynx_cid)
                    cust_pw = (cust or {}).get("password", "").strip()
                    if cust_pw:
                        for login in logins_needing_fallback:
                            pw_map.setdefault(login, cust_pw)
                        stats["fallback_customer_password"] += len(logins_needing_fallback)

                with rconn.cursor() as cur:
                    for sub in sub_list:
                        cleartext = pw_map.get(sub.login)
                        if not cleartext:
                            stats["skipped_no_password_in_api"] += 1
                            continue
                        try:
                            _upsert_access_credential(
                                db,
                                sub.subscriber_id,
                                sub.login,
                                cleartext,
                                sub.radius_profile_id,
                            )
                            n_attrs = _upsert_radius_rows(
                                cur, sub, cleartext, sub.offer, sub.radius_profile
                            )
                            stats["access_creds_upserted"] += 1
                            stats["radcheck_upserts"] += 1
                            stats["radreply_attrs_written"] += n_attrs
                            if sub.status == SubscriptionStatus.blocked:
                                stats["blocked_users"] += 1
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(
                                "upsert failed for %s: %s", sub.login, exc
                            )
                            stats["upsert_failures"] += 1
                            # CRITICAL: rollback the SQLAlchemy session to avoid
                            # "Can't reconnect until invalid transaction is rolled back"
                            # cascade — otherwise ALL subsequent customers fail too.
                            try: db.rollback()
                            except Exception: pass
                            try: rconn.rollback()
                            except Exception: pass

                processed_customers += 1
                # Commit every 250 customers to keep transactions small
                if processed_customers % 250 == 0:
                    db.flush()
                    db.commit()
                    rconn.commit()
                    logger.info(
                        "progress %d/%d customers, stats=%s",
                        processed_customers,
                        len(subs_by_splynx_cid),
                        dict(stats),
                    )
                    time.sleep(0.1)  # gentle on the API

            # Final commit
            db.commit()
            rconn.commit()
            logger.info("FINAL stats: %s", dict(stats))
        finally:
            rconn.close()
    finally:
        db.close()

    return dict(stats)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--execute", action="store_true", help="Live run (default: dry-run)")
    p.add_argument("--limit", type=int, help="Process only first N subscriptions (testing)")
    p.add_argument(
        "--missing-only",
        action="store_true",
        help="Only process subs missing a usable AccessCredential (no row OR decrypt-fail)",
    )
    p.add_argument(
        "--customer-ids",
        type=str,
        help="Comma-separated Splynx customer IDs to restrict processing to (testing)",
    )
    p.add_argument(
        "--prefer-customer-password",
        action="store_true",
        help="Always prefer customers.password over services_internet.password "
             "(matches what Splynx_radd actually authenticates against in MS-CHAPv2 mode)",
    )
    args = p.parse_args()
    cust_filter: set[int] | None = None
    if args.customer_ids:
        cust_filter = {int(x.strip()) for x in args.customer_ids.split(",") if x.strip()}
    run(
        dry_run=not args.execute,
        limit=args.limit,
        missing_only=args.missing_only,
        customer_ids=cust_filter,
        prefer_customer_password=args.prefer_customer_password,
    )
