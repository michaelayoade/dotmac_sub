"""Populate radcheck + radreply from dotmac_sub authoritative joins.

Source of truth: dotmac_sub Postgres
  - Subscription (login, status, ipv4_address, offer_id, radius_profile_id)
  - AccessCredential (username, secret_hash — Fernet-encrypted)
  - CatalogOffer (speed_download_mbps, speed_upload_mbps)
  - RadiusProfile (mikrotik_rate_limit, idle_timeout, simultaneous_use)

No Splynx API calls. No double-source. Idempotent (DELETE + INSERT per user).

After the one-time Splynx-API password bootstrap (radius_sync.py
update_access_credentials), this script can run anytime to (re)populate the
RADIUS DB from dotmac_sub. It also runs as the implementation behind the
Celery handler for subscription change events (see TODO at bottom).

Usage:
  docker exec -e PYTHONPATH=/app -w /app dotmac_sub_app \\
      python -m scripts.migration.populate_radius_from_subs --execute
"""

from __future__ import annotations

import logging
import os
import sys

import psycopg
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
from app.services.credential_crypto import (
    decrypt_credential_with_key,
    get_encryption_key,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

ACCT_INTERIM_SECONDS = 300  # 5 min Acct-Interim-Update cadence
SUSPENDED_ADDRESS_LIST = "suspended"  # MikroTik address-list for blocked customers


def _rate_limit(offer: CatalogOffer, profile: RadiusProfile | None) -> str | None:
    """Pick MikroTik rate-limit string: profile override > offer-derived > None."""
    if profile and profile.mikrotik_rate_limit:
        return profile.mikrotik_rate_limit
    if offer and offer.speed_download_mbps and offer.speed_upload_mbps:
        return f"{offer.speed_download_mbps}M/{offer.speed_upload_mbps}M"
    return None


def _radreply_attrs(
    sub: Subscription,
    offer: CatalogOffer,
    profile: RadiusProfile | None,
    subscriber_blocked: bool = False,
    captive_redirect_enabled: bool = False,
) -> list[tuple[str, str, str]]:
    """Compute the list of (attribute, op, value) tuples for radreply.

    `subscriber_blocked`: customer-level block (Splynx subscribers.status=blocked).
    Customer-level block dominates: even if subscription is active, the customer
    gets blocked RADIUS treatment.

    `captive_redirect_enabled`: per-customer opt-in for the soft walled-garden
    captive redirect. Only opted-in blocked subscribers get the
    Mikrotik-Address-List=suspended attribute; non-opted blocked subscribers are
    hard-rejected in radcheck (see populate()), so they get no captive radreply.
    """
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

    # Soft captive walled-garden — only for blocked subscribers who OPTED IN
    # (per-customer captive_redirect_enabled). Non-opted blocked subscribers get
    # a hard reject in radcheck instead, so they never reach this radreply.
    is_blocked = subscriber_blocked or sub.status in (
        SubscriptionStatus.blocked,
        SubscriptionStatus.suspended,
    )
    if is_blocked and captive_redirect_enabled:
        attrs.append(("Mikrotik-Address-List", ":=", SUSPENDED_ADDRESS_LIST))

    return attrs


def populate(dry_run: bool = True) -> dict[str, int]:
    radius_dsn = os.environ.get("RADIUS_DB_DSN", "")
    if not radius_dsn:
        raise RuntimeError("RADIUS_DB_DSN not set")

    stats = {
        "subscriptions_considered": 0,
        "skipped_no_credential": 0,
        "skipped_no_password": 0,
        "skipped_decrypt_failed": 0,
        "radcheck_upserts": 0,
        "radreply_upserts": 0,
        "blocked_users_written": 0,
    }

    enc_key = get_encryption_key()
    db = SessionLocal()
    try:
        # Active, blocked, or suspended subs with a login — blocked/suspended
        # subs get a walled-garden radreply so suspension actually takes
        # effect at the BNG (hard-deleting their rows would fail-closed but
        # lose the captive pay-page treatment).
        rows = (
            db.execute(
                select(Subscription)
                .options(
                    joinedload(Subscription.offer),
                    joinedload(Subscription.radius_profile),
                )
                .where(
                    Subscription.status.in_(
                        [
                            SubscriptionStatus.active,
                            SubscriptionStatus.blocked,
                            SubscriptionStatus.suspended,
                        ]
                    ),
                    Subscription.login.isnot(None),
                )
            )
            .unique()
            .scalars()
            .all()
        )
        stats["subscriptions_considered"] = len(rows)
        logger.info(
            "considering %d active/blocked subscriptions with a login", len(rows)
        )

        # Pre-fetch all AccessCredentials keyed by username
        creds_by_username: dict[str, AccessCredential] = {
            c.username: c
            for c in db.scalars(
                select(AccessCredential).where(AccessCredential.is_active.is_(True))
            ).all()
        }

        # Pre-fetch blocked subscriber IDs — customer-level block triggers
        # walled-garden regardless of subscription status. Source of truth
        # is Splynx customers.status mirrored into Subscriber.status by
        # scripts/migration/sync_subscriber_status_from_splynx.py
        from app.models.subscriber import Subscriber, SubscriberStatus

        blocked_subscriber_ids: set = {
            sid
            for (sid,) in db.execute(
                select(Subscriber.id).where(
                    Subscriber.status == SubscriberStatus.blocked
                )
            ).all()
        }
        logger.info(
            "%d subscribers in blocked state",
            len(blocked_subscriber_ids),
        )

        # Per-customer opt-in for the soft captive redirect. Blocked subscribers
        # NOT in this set are hard-rejected (Auth-Type := Reject) instead of
        # walled-gardened — the captive redirect is opt-in, not every account.
        captive_optin_ids: set = {
            sid
            for (sid,) in db.execute(
                select(Subscriber.id).where(
                    Subscriber.captive_redirect_enabled.is_(True)
                )
            ).all()
        }
        logger.info(
            "%d subscribers opted into captive redirect", len(captive_optin_ids)
        )

        # Compute the full work list in memory while the dotmac session is
        # alive, then release it BEFORE the radius writes — holding the read
        # transaction through the write phase trips the app's 120s
        # idle-in-transaction timeout on large fleets.
        by_login: dict[str, tuple[str, str, list, bool, SubscriptionStatus, str]] = {}
        for sub in rows:
            cred = creds_by_username.get(sub.login)
            if cred is None:
                stats["skipped_no_credential"] += 1
                continue
            if not cred.secret_hash:
                stats["skipped_no_password"] += 1
                continue
            try:
                cleartext = decrypt_credential_with_key(cred.secret_hash, enc_key)
            except Exception as exc:  # noqa: BLE001
                logger.warning("decrypt failed for %s: %s", sub.login, exc)
                stats["skipped_decrypt_failed"] += 1
                continue
            if not cleartext:
                stats["skipped_no_password"] += 1
                continue

            sub_blocked = sub.subscriber_id in blocked_subscriber_ids
            captive = sub.subscriber_id in captive_optin_ids
            attrs = _radreply_attrs(
                sub,
                sub.offer,
                sub.radius_profile,
                sub_blocked,
                captive_redirect_enabled=captive,
            )
            blocked_flag = sub_blocked or sub.status in (
                SubscriptionStatus.blocked,
                SubscriptionStatus.suspended,
            )
            # Enforcement mode for the radcheck write: active subs and opted-in
            # blocked subs keep a usable password (captive subs are walled via
            # the radreply Address-List); non-opted blocked subs are hard
            # rejected (Auth-Type := Reject, offline).
            if blocked_flag and not captive:
                mode = "reject"
            elif blocked_flag:
                mode = "captive"
            else:
                mode = "active"
            # Duplicate logins (Splynx-migration dups): the ACTIVE sub wins the
            # slot — subscriber-level block still dominates via sub_blocked,
            # so a blocked customer stays enforced either way.
            existing = by_login.get(sub.login)
            if existing is not None and existing[4] == SubscriptionStatus.active:
                continue
            by_login[sub.login] = (
                sub.login,
                cleartext,
                attrs,
                blocked_flag,
                sub.status,
                mode,
            )

        active_usernames = {sub.login for sub in rows if sub.login}
    finally:
        db.close()

    work = list(by_login.values())
    stats["radcheck_upserts"] = len(work)
    stats["radreply_upserts"] = sum(len(w[2]) for w in work)
    stats["blocked_users_written"] = sum(1 for w in work if w[3])
    stats["captive_users_written"] = sum(1 for w in work if w[5] == "captive")
    stats["rejected_users_written"] = sum(1 for w in work if w[5] == "reject")

    if dry_run:
        logger.info("DRY RUN — no writes (orphan cleanup also skipped)")
        logger.info("done: %s", stats)
        return stats

    rconn = psycopg.connect(radius_dsn)
    rconn.autocommit = False
    try:
        with rconn.cursor() as cur:
            usernames = [w[0] for w in work]
            cur.execute("DELETE FROM radcheck WHERE username = ANY(%s)", (usernames,))
            # Non-rejected users authenticate with their Cleartext-Password
            # (active normally, opted-in-blocked into the captive walled-garden
            # via radreply). Hard-rejected users get a single Auth-Type := Reject
            # row so FreeRADIUS refuses them — no password, fully offline.
            password_rows = [(w[0], w[1]) for w in work if w[5] != "reject"]
            reject_rows = [(w[0],) for w in work if w[5] == "reject"]
            if password_rows:
                cur.executemany(
                    "INSERT INTO radcheck (username, attribute, op, value) "
                    "VALUES (%s, 'Cleartext-Password', ':=', %s)",
                    password_rows,
                )
            if reject_rows:
                cur.executemany(
                    "INSERT INTO radcheck (username, attribute, op, value) "
                    "VALUES (%s, 'Auth-Type', ':=', 'Reject')",
                    reject_rows,
                )
            cur.execute("DELETE FROM radreply WHERE username = ANY(%s)", (usernames,))
            # Hard-rejected users get no radreply (they never authenticate).
            cur.executemany(
                "INSERT INTO radreply (username, attribute, op, value) "
                "VALUES (%s, %s, %s, %s)",
                [
                    (w[0], a, o, v)
                    for w in work
                    if w[5] != "reject"
                    for (a, o, v) in w[2]
                ],
            )

            # --- orphan cleanup: drop radcheck/radreply rows whose username ---
            # is not in the active+blocked set (e.g. subs that have since been
            # cancelled/disabled). Keeps the radius DB lean and prevents stale
            # auth surface from accumulating.
            if active_usernames:
                cur.execute("SELECT DISTINCT username FROM radcheck")
                radcheck_users = {r[0] for r in cur.fetchall()}
                orphans = list(radcheck_users - active_usernames)
                if orphans:
                    cur.execute(
                        "DELETE FROM radcheck WHERE username = ANY(%s)", (orphans,)
                    )
                    stats["radcheck_orphans_deleted"] = cur.rowcount
                    cur.execute(
                        "DELETE FROM radreply WHERE username = ANY(%s)", (orphans,)
                    )
                    stats["radreply_orphans_deleted"] = cur.rowcount
                    logger.info(
                        "orphan cleanup: %d radcheck + %d radreply rows",
                        stats["radcheck_orphans_deleted"],
                        stats["radreply_orphans_deleted"],
                    )

        rconn.commit()
        logger.info("committed RADIUS DB writes")
    finally:
        rconn.close()

    logger.info("done: %s", stats)
    return stats


if __name__ == "__main__":
    if "--execute" in sys.argv:
        populate(dry_run=False)
    else:
        populate(dry_run=True)
        print(
            "\nTo execute: python -m scripts.migration.populate_radius_from_subs --execute"
        )


# -----------------------------------------------------------------------------
# TODO — Celery change-driven handler (Step 3 of the migration plan)
#
# Replace the periodic resync with event-driven updates. When a subscription
# changes (status/login/ipv4/offer/profile) or its AccessCredential changes
# (password rotation), a small per-user upsert runs instead of a full scan.
#
# Sketch:
#
#   @celery_app.task(name="app.tasks.radius.upsert_one")
#   def upsert_one(subscription_id: str) -> None:
#       db = SessionLocal()
#       sub = db.get(Subscription, subscription_id)
#       if not sub or sub.login is None:
#           return
#       cred = db.scalar(select(AccessCredential)
#                        .where(AccessCredential.username == sub.login))
#       # ... same per-user logic from populate(), but for one sub
#
#   # In app/services/events/handlers/subscription.py:
#   def on_subscription_changed(event):
#       upsert_one.delay(str(event.subscription_id))
#
#   def on_access_credential_changed(event):
#       # find subs sharing this username, queue each
#       ...
#
# Should also handle delete: when a sub is canceled/terminated, DELETE the
# radcheck + radreply rows so freeradius rejects future auths.
# -----------------------------------------------------------------------------
