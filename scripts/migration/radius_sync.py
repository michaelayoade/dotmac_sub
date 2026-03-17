"""RADIUS sync: Pull cleartext passwords from Splynx API and populate FreeRADIUS DB.

Uses the Splynx REST API to get decrypted service passwords, then writes:
1. radcheck: username + Cleartext-Password
2. radreply: speed limits, IP, service attributes
3. nas: NAS clients with shared secrets

Connects to:
- Splynx API: https://138.68.165.175 (selfcare.dotmac.ng)
- RADIUS DB: PostgreSQL on localhost:5437 (radius database)
- DotMac Sub DB: PostgreSQL on localhost:5434 (dotmac_sub)
"""

from __future__ import annotations

import logging
import sys
import time

import psycopg
import requests
import urllib3

from scripts.migration.db_connections import dotmac_session

# Suppress SSL warnings for self-signed cert
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# --- Splynx API config ---
SPLYNX_API_BASE = "https://138.68.165.175"
SPLYNX_API_KEY = "231cea3f36e218736b7339489477641f"
SPLYNX_API_SECRET = "6ecb2ec96216809c065a64c864870a27"
SPLYNX_HOST_HEADER = "selfcare.dotmac.ng"

# --- RADIUS DB config ---
RADIUS_DB_DSN = "postgresql://radius:l2f3clS-Ws9WgTXcsW3HoznBnEq3n7N-@localhost:5437/radius"


def _splynx_get(endpoint: str, params: dict | None = None) -> list | dict | None:
    """Make authenticated GET request to Splynx API."""
    url = f"{SPLYNX_API_BASE}/api/2.0/admin/{endpoint}"
    resp = requests.get(
        url,
        params=params,
        auth=(SPLYNX_API_KEY, SPLYNX_API_SECRET),
        headers={"Host": SPLYNX_HOST_HEADER, "Content-Type": "application/json"},
        verify=False,
        timeout=30,
    )
    if resp.status_code == 200:
        return resp.json()
    if resp.status_code == 404:
        return None
    logger.warning("API %s returned %d: %s", endpoint, resp.status_code, resp.text[:200])
    return None


def sync_nas_clients() -> int:
    """Sync NAS devices to RADIUS nas table."""
    conn = psycopg.connect(RADIUS_DB_DSN)
    cur = conn.cursor()

    # Get NAS devices from DotMac Sub with Splynx metadata
    with dotmac_session() as db:
        from sqlalchemy import select

        from app.models.splynx_mapping import SplynxEntityType, SplynxIdMapping

        mappings = db.execute(
            select(SplynxIdMapping).where(
                SplynxIdMapping.entity_type == SplynxEntityType.router
            )
        ).scalars().all()

        created = 0
        for m in mappings:
            meta = m.metadata_ or {}
            nasname = meta.get("nas_ip") or meta.get("ip")
            shortname = meta.get("title", "")[:32]
            secret = meta.get("radius_secret", "")
            if not nasname or not secret:
                continue

            # Upsert
            cur.execute("DELETE FROM nas WHERE nasname = %s", (nasname,))
            cur.execute(
                "INSERT INTO nas (nasname, shortname, type, secret, description) "
                "VALUES (%s, %s, %s, %s, %s)",
                (nasname, shortname, "other", secret, f"Splynx router {m.splynx_id}"),
            )
            created += 1

    conn.commit()
    conn.close()
    logger.info("NAS clients: %d synced to RADIUS DB", created)
    return created


def sync_service_passwords(batch_size: int = 50) -> dict[str, int]:
    """Pull cleartext service passwords from Splynx API and write to radcheck + radreply."""
    conn = psycopg.connect(RADIUS_DB_DSN)
    cur = conn.cursor()

    # Get all active customer IDs from Splynx mappings
    with dotmac_session() as db:
        from sqlalchemy import select

        from app.models.catalog import Subscription, SubscriptionStatus
        from app.models.splynx_mapping import SplynxEntityType, SplynxIdMapping

        # Get customers with active subscriptions
        active_sub_customer_ids = set(
            db.scalars(
                select(Subscription.splynx_service_id).where(
                    Subscription.status == SubscriptionStatus.active,
                    Subscription.splynx_service_id.isnot(None),
                    Subscription.login.isnot(None),
                )
            ).all()
        )

        # Get Splynx customer IDs that have active services
        customer_mappings = {
            m.splynx_id: m.dotmac_id
            for m in db.scalars(
                select(SplynxIdMapping).where(
                    SplynxIdMapping.entity_type == SplynxEntityType.customer
                )
            ).all()
        }

    # Get unique customer IDs with active services from Splynx
    # We need to query the API per-customer for their internet services
    logger.info("Fetching service passwords for %d customers with active services", len(customer_mappings))

    created = 0
    skipped = 0
    errors = 0
    processed = 0

    # Process all customers - the API returns services per customer
    customer_ids = sorted(customer_mappings.keys())

    for cid in customer_ids:
        try:
            services = _splynx_get(f"customers/customer/{cid}/internet-services")
            if not services or not isinstance(services, list):
                skipped += 1
                processed += 1
                continue

            for svc in services:
                login = svc.get("login", "").strip()
                password = svc.get("password", "").strip()
                status = svc.get("status", "")

                if not login or not password:
                    skipped += 1
                    continue

                if status not in ("active", "blocked"):
                    skipped += 1
                    continue

                # Write to radcheck
                cur.execute("DELETE FROM radcheck WHERE username = %s", (login,))
                cur.execute(
                    "INSERT INTO radcheck (username, attribute, op, value) "
                    "VALUES (%s, 'Cleartext-Password', ':=', %s)",
                    (login, password),
                )

                # Write to radreply - speed limits and IP
                cur.execute("DELETE FROM radreply WHERE username = %s", (login,))

                # Service-Type and Framed-Protocol for PPPoE
                reply_attrs = [
                    ("Service-Type", ":=", "Framed-User"),
                    ("Framed-Protocol", ":=", "PPP"),
                ]

                # IP address
                ipv4 = svc.get("ipv4", "").strip()
                if ipv4 and ipv4 != "0.0.0.0":
                    reply_attrs.append(("Framed-IP-Address", ":=", ipv4))

                # Speed limits (MikroTik rate-limit format)
                # Splynx stores in kbps, MikroTik expects k format
                download = svc.get("speed_download") or svc.get("tariff_speed_download")
                upload = svc.get("speed_upload") or svc.get("tariff_speed_upload")

                # Simultaneous-Use
                reply_attrs.append(("Simultaneous-Use", ":=", "1"))

                for attr, op, val in reply_attrs:
                    cur.execute(
                        "INSERT INTO radreply (username, attribute, op, value) "
                        "VALUES (%s, %s, %s, %s)",
                        (login, attr, op, str(val)),
                    )

                created += 1

        except Exception as e:
            errors += 1
            if errors <= 5:
                logger.warning("Error processing customer %d: %s", cid, e)

        processed += 1
        if processed % 500 == 0:
            conn.commit()
            logger.info(
                "Progress: %d/%d customers, %d credentials created, %d skipped, %d errors",
                processed, len(customer_ids), created, skipped, errors,
            )
            time.sleep(0.1)  # Gentle on the API

    conn.commit()
    conn.close()

    # Also update access_credentials in DotMac Sub with cleartext passwords
    logger.info(
        "RADIUS sync complete: %d credentials, %d skipped, %d errors",
        created, skipped, errors,
    )
    return {"created": created, "skipped": skipped, "errors": errors}


def update_access_credentials(batch_size: int = 50) -> int:
    """Update DotMac Sub access_credentials with cleartext passwords from Splynx API."""
    updated = 0

    with dotmac_session() as db:
        from sqlalchemy import select

        from app.models.catalog import AccessCredential

        # Get credentials that have encrypted (non-cleartext) passwords
        creds = db.scalars(
            select(AccessCredential).where(
                AccessCredential.is_active.is_(True),
                AccessCredential.secret_hash.isnot(None),
            )
        ).all()

        # Group by subscriber to batch API calls
        from collections import defaultdict
        sub_creds: dict[str, list] = defaultdict(list)
        for cred in creds:
            sub_creds[str(cred.subscriber_id)].append(cred)

        # Get subscriber → splynx_customer_id mapping
        from app.models.subscriber import Subscriber
        sub_to_splynx = {
            str(s.id): s.splynx_customer_id
            for s in db.scalars(
                select(Subscriber).where(
                    Subscriber.splynx_customer_id.isnot(None)
                )
            ).all()
        }

        processed = 0
        for sub_id, cred_list in sub_creds.items():
            splynx_cid = sub_to_splynx.get(sub_id)
            if not splynx_cid:
                continue

            services = _splynx_get(f"customers/customer/{splynx_cid}/internet-services")
            if not services or not isinstance(services, list):
                continue

            # Build login → cleartext password map
            pw_map = {
                svc["login"].strip(): svc["password"].strip()
                for svc in services
                if svc.get("login") and svc.get("password")
            }

            for cred in cred_list:
                cleartext = pw_map.get(cred.username)
                if cleartext:
                    # Store as enc: format so the app knows it's reversible
                    from app.services.credential_crypto import encrypt_credential
                    cred.secret_hash = encrypt_credential(cleartext)
                    updated += 1

            processed += 1
            if processed % 500 == 0:
                db.flush()
                logger.info("Updated %d credentials (%d customers processed)", updated, processed)
                time.sleep(0.1)

        db.commit()

    logger.info("Access credentials updated: %d", updated)
    return updated


def run_radius_sync(dry_run: bool = True) -> None:
    """Execute full RADIUS sync."""
    logger.info("=== RADIUS Sync ===")

    if dry_run:
        # Test API connectivity
        result = _splynx_get("customers/customer/5")
        if result:
            logger.info("API connected. Customer 5: %s (password: %s)",
                       result.get("name"), result.get("password", "")[:4] + "****")
        else:
            logger.error("API connection failed")
            return

        # Count what we'd sync
        logger.info("DRY RUN — would sync:")
        with dotmac_session() as db:
            from sqlalchemy import func, select

            from app.models.catalog import AccessCredential

            total = db.scalar(select(func.count(AccessCredential.id)).where(
                AccessCredential.is_active.is_(True)
            ))
            logger.info("  Active credentials: %d", total)
        logger.info("Run with --execute to sync")
        return

    # Step 1: Sync NAS clients
    sync_nas_clients()

    # Step 2: Sync service passwords to radcheck + radreply
    result = sync_service_passwords()

    # Step 3: Update access_credentials with cleartext passwords
    update_access_credentials()

    logger.info("=== RADIUS sync complete ===")


if __name__ == "__main__":
    if "--execute" in sys.argv:
        run_radius_sync(dry_run=False)
    else:
        run_radius_sync(dry_run=True)
        print("\nTo execute: poetry run python -m scripts.migration.radius_sync --execute")
