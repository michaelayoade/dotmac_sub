"""Bootstrap customer portal credentials from Splynx → dotmac UserCredential.

For each dotmac Subscriber with a splynx_customer_id:
  - Pull customer's portal password from Splynx API (`customers/customer/{id}`)
  - Hash it (passlib pbkdf2_sha256)
  - UPSERT into UserCredential with provider=local, username=customer login

This enables customers to log into dotmac's `/customer/*` portal with the
same password they use today on selfcare.dotmac.ng. No password reset email
needed for the cutover.

Idempotent: skip subscribers that already have a UserCredential row.
Includes per-subscriber try/except + rollback to avoid session-poisoning
cascades (lesson learned from radius bootstrap).

Usage:
    docker exec -e PYTHONPATH=/app -w /app dotmac_sub_app \\
        python -m app.services.migrations.bootstrap_portal_credentials
    docker exec -e PYTHONPATH=/app -w /app dotmac_sub_app \\
        python -m app.services.migrations.bootstrap_portal_credentials --execute
    # Bound to active customers only (recommended for dual-run):
    docker exec -e PYTHONPATH=/app -w /app dotmac_sub_app \\
        python -m app.services.migrations.bootstrap_portal_credentials --execute --active-only
"""

from __future__ import annotations

import argparse
import logging
import os
import time
import uuid
from collections import Counter
from datetime import UTC, datetime

import requests
import urllib3
from sqlalchemy import select

from app.db import SessionLocal
from app.models.auth import AuthProvider, UserCredential
from app.models.catalog import Subscription, SubscriptionStatus
from app.models.subscriber import Subscriber
from app.services.auth_flow import hash_password

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

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
    """Fetch a customer record (incl. cleartext portal password)."""
    if not (SPLYNX_API_BASE and SPLYNX_API_KEY and SPLYNX_API_SECRET):
        raise RuntimeError(
            "SPLYNX_API_BASE/KEY/SECRET must be set in dotmac_sub_app env"
        )
    url = f"{SPLYNX_API_BASE}/api/2.0/admin/customers/customer/{customer_id}"
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
        if r.status_code == 200:
            return r.json() if isinstance(r.json(), dict) else None
    except requests.RequestException as exc:
        logger.warning("API exception for customer %d: %s", customer_id, exc)
    return None


def run(dry_run: bool = True, active_only: bool = True) -> dict[str, int]:
    stats: Counter = Counter()
    db = SessionLocal()
    try:
        # 1. Pick subscribers in scope (active customers preferred for dual-run)
        if active_only:
            # subscribers with at least one active/blocked subscription with login
            sub_ids_with_active = {
                sid
                for (sid,) in db.execute(
                    select(Subscription.subscriber_id).where(
                        Subscription.status.in_(
                            [SubscriptionStatus.active, SubscriptionStatus.blocked]
                        ),
                        Subscription.login.isnot(None),
                    )
                ).all()
            }
            subscribers = db.scalars(
                select(Subscriber).where(
                    Subscriber.splynx_customer_id.isnot(None),
                    Subscriber.id.in_(sub_ids_with_active),
                )
            ).all()
        else:
            subscribers = db.scalars(
                select(Subscriber).where(Subscriber.splynx_customer_id.isnot(None))
            ).all()
        stats["subscribers_in_scope"] = len(subscribers)
        logger.info("subscribers in scope: %d", len(subscribers))

        # 2. Pre-fetch existing UserCredential rows to skip duplicates
        existing_subs = {
            s
            for (s,) in db.execute(
                select(UserCredential.subscriber_id).where(
                    UserCredential.provider == AuthProvider.local,
                    UserCredential.subscriber_id.isnot(None),
                )
            ).all()
        }
        existing_usernames = {
            u
            for (u,) in db.execute(
                select(UserCredential.username).where(
                    UserCredential.provider == AuthProvider.local,
                    UserCredential.username.isnot(None),
                )
            ).all()
        }
        stats["already_have_credential"] = sum(
            1 for s in subscribers if s.id in existing_subs
        )

        candidates = [s for s in subscribers if s.id not in existing_subs]
        stats["new_to_create"] = len(candidates)
        logger.info(
            "already have credential: %d. new to create: %d",
            stats["already_have_credential"],
            stats["new_to_create"],
        )

        if not candidates:
            return dict(stats)

        if dry_run:
            logger.info("DRY RUN — would call API for %d customers", len(candidates))
            return dict(stats)

        # 3. Process each candidate
        for i, sub in enumerate(candidates, 1):
            try:
                if sub.splynx_customer_id is None:
                    stats["skipped_no_splynx_id"] = (
                        stats.get("skipped_no_splynx_id", 0) + 1
                    )
                    continue
                cust = _splynx_customer(sub.splynx_customer_id)
                if cust is None:
                    stats["api_failure"] += 1
                    continue
                cleartext = (cust.get("password") or "").strip()
                if not cleartext:
                    stats["skipped_no_password"] += 1
                    continue
                # Use customer.login as the portal username (matches what they
                # type today on selfcare.dotmac.ng). Fall back to email if absent.
                username = (cust.get("login") or "").strip() or (
                    cust.get("email") or ""
                ).strip()
                if not username:
                    stats["skipped_no_username"] += 1
                    continue
                if username in existing_usernames:
                    # Another subscriber's credential already owns this username.
                    # Most likely a dup-login situation; skip to avoid the unique
                    # index conflict.
                    stats["skipped_username_collision"] += 1
                    continue

                cred = UserCredential(
                    id=uuid.uuid4(),
                    subscriber_id=sub.id,
                    system_user_id=None,
                    provider=AuthProvider.local,
                    username=username,
                    password_hash=hash_password(cleartext),
                    must_change_password=False,
                    is_active=True,
                    password_updated_at=datetime.now(UTC),
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
                db.add(cred)
                existing_usernames.add(username)
                stats["created"] += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "create failed for subscriber %s (splynx %s): %s",
                    sub.id,
                    sub.splynx_customer_id,
                    exc,
                )
                stats["create_failures"] += 1
                # CRITICAL: roll back to avoid session-poisoning cascade
                try:
                    db.rollback()
                except Exception:
                    pass

            # Commit every 100 for safety
            if i % 100 == 0:
                db.commit()
                logger.info(
                    "progress %d/%d, stats=%s",
                    i,
                    len(candidates),
                    dict(stats),
                )
                time.sleep(0.05)

        db.commit()
        logger.info("FINAL stats: %s", dict(stats))
    finally:
        db.close()

    return dict(stats)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--execute", action="store_true", help="Live run (default: dry-run)")
    p.add_argument(
        "--active-only",
        action="store_true",
        default=True,
        help="Limit to subscribers with at least one active/blocked subscription (default true)",
    )
    p.add_argument(
        "--all",
        dest="active_only",
        action="store_false",
        help="Process ALL subscribers with splynx_customer_id (slower)",
    )
    args = p.parse_args()
    run(dry_run=not args.execute, active_only=args.active_only)
