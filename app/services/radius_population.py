"""Populate radcheck + radreply from dotmac_sub authoritative joins.

Source of truth: dotmac_sub Postgres
  - Subscription (login, status, ipv4_address, offer_id, radius_profile_id)
  - AccessCredential (username, secret_hash — Fernet-encrypted)
  - CatalogOffer (speed_download_mbps, speed_upload_mbps)
  - RadiusProfile (mikrotik_rate_limit, idle_timeout, simultaneous_use)

No external BSS calls. No double-source. Idempotent (DELETE + INSERT per user).

Usage:
  docker exec -e PYTHONPATH=/app -w /app dotmac_sub_app \\
      python -m app.services.radius_population --execute
"""

from __future__ import annotations

import logging
import sys
from typing import cast

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
from app.services.radius_dsn import radius_dsn_libpq

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
    additional_routes: list[tuple[str, int | None]] | None = None,
    framed_ipv4: str | None = None,
    framed_ipv6: str | None = None,
    delegated_ipv6: str | None = None,
) -> list[tuple[str, str, str]]:
    """Compute the list of (attribute, op, value) tuples for radreply.

    `subscriber_blocked`: customer-level block.
    Customer-level block dominates: even if subscription is active, the customer
    gets blocked RADIUS treatment.

    `captive_redirect_enabled`: per-customer opt-in for the soft walled-garden
    captive redirect. Only opted-in blocked subscribers get the
    Mikrotik-Address-List=suspended attribute; non-opted blocked subscribers are
    hard-rejected in radcheck (see populate()), so they get no captive radreply.

    `additional_routes`: extra routed IP blocks (subscriber_additional_routes) as
    (cidr, metric) tuples. Emitted as Framed-Route for non-walled-garden subs only
    — this is the authoritative single-writer, so the routes must be emitted here
    or the periodic sweep wipes what build_radius_reply_attributes wrote.

    `framed_ipv4`: the IP to emit as Framed-IP-Address. Defaults to
    `sub.ipv4_address`, but the caller passes an active-IPAssignment fallback so a
    stale/cleared `subscriptions.ipv4_address` does NOT silently drop Framed-IP
    (which de-IPs the customer and the BNG tears the session down). "0.0.0.0" is
    treated as no address.

    `framed_ipv6`: the prefix to emit as Framed-IPv6-Prefix; defaults to
    `sub.ipv6_address`. This authoritative sweep must emit it too — otherwise the
    sweep's `DELETE FROM radreply` wipes the Framed-IPv6-Prefix that
    build_radius_reply_attributes wrote on activation, so IPv6 RADIUS could never
    be durable (same wipe hazard the Framed-Route handling already guards against).
    """
    ipv4 = framed_ipv4 if framed_ipv4 is not None else sub.ipv4_address
    if ipv4 == "0.0.0.0":  # nosec B104  # noqa: S104 — IP-string compare, not a bind
        ipv4 = None
    ipv6 = (
        framed_ipv6 if framed_ipv6 is not None else getattr(sub, "ipv6_address", None)
    )
    ipv6 = (str(ipv6).strip() or None) if ipv6 else None

    attrs: list[tuple[str, str, str]] = [
        ("Service-Type", ":=", "Framed-User"),
        ("Framed-Protocol", ":=", "PPP"),
        ("Acct-Interim-Interval", ":=", str(ACCT_INTERIM_SECONDS)),
    ]

    if ipv4:
        attrs.append(("Framed-IP-Address", ":=", ipv4))
    if ipv6:
        attrs.append(("Framed-IPv6-Prefix", ":=", ipv6))
    delegated = (str(delegated_ipv6).strip() or None) if delegated_ipv6 else None
    if delegated:
        attrs.append(("Delegated-IPv6-Prefix", ":=", delegated))

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
    elif additional_routes and not is_blocked:
        # Additional routed IP blocks -> one Framed-Route each (+= so multiple
        # coexist; gateway 0.0.0.0 = via this session, since primaries are CGNAT).
        # Not for walled-garden subs: a captive customer must not route extra IPs.
        primary_host = f"{ipv4}/32" if ipv4 else None
        seen: set[str] = set()
        for cidr, metric in additional_routes:
            cidr = (cidr or "").strip()
            if not cidr or cidr == primary_host or cidr in seen:
                continue
            seen.add(cidr)
            attrs.append(("Framed-Route", "+=", f"{cidr} 0.0.0.0 {metric or 1}"))

    return attrs


def populate(dry_run: bool = True) -> dict[str, int]:
    # Single authority (shared with radius.py's event-time sync) so both writers
    # target the same radius DB and cannot split-brain.
    radius_dsn = radius_dsn_libpq()
    if not radius_dsn:
        raise RuntimeError("RADIUS database DSN not configured")

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

        # Pre-fetch blocked subscriber IDs. Customer-level block triggers
        # walled-garden regardless of subscription status.
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

        # Pre-fetch additional routed IP blocks,
        # keyed by subscriber_id, so each user's Framed-Routes are O(1) in the
        # sweep loop below.
        from app.models.network import SubscriberAdditionalRoute

        routes_by_subscriber: dict = {}
        for r in db.scalars(
            select(SubscriberAdditionalRoute).where(
                SubscriberAdditionalRoute.is_active.is_(True)
            )
        ).all():
            routes_by_subscriber.setdefault(r.subscriber_id, []).append(
                (r.cidr, r.metric)
            )
        logger.info(
            "%d subscribers with additional routed IP blocks",
            len(routes_by_subscriber),
        )

        # Fallback IPv4 from the active IPAssignment so a stale/cleared
        # subscriptions.ipv4_address doesn't silently drop Framed-IP-Address (which
        # de-IPs the customer -> BNG teardown -> reconnect flap). One query, keyed
        # by subscriber_id.
        from app.models.network import IPAssignment, IPv4Address, IPVersion

        ipv4_by_subscriber: dict = {}
        for sid, addr in db.execute(
            select(IPAssignment.subscriber_id, IPv4Address.address)
            .join(IPv4Address, IPAssignment.ipv4_address_id == IPv4Address.id)
            .where(IPAssignment.is_active.is_(True))
            .where(IPAssignment.ip_version == IPVersion.ipv4)
        ).all():
            if sid and addr:
                ipv4_by_subscriber.setdefault(sid, str(addr))

        # IPv6 PD: the subscriber's assigned delegated prefix, emitted as
        # Delegated-IPv6-Prefix. Flag-gated (inert until IPv6 PD is turned on).
        pd_by_subscriber: dict = {}
        from app.services.ipv6_pd import pd_enabled

        if pd_enabled():
            from app.models.network import Ipv6DelegatedPrefix, Ipv6PrefixState

            for sid, prefix, plen in db.execute(
                select(
                    Ipv6DelegatedPrefix.subscriber_id,
                    Ipv6DelegatedPrefix.prefix,
                    Ipv6DelegatedPrefix.prefix_length,
                ).where(Ipv6DelegatedPrefix.state == Ipv6PrefixState.assigned)
            ).all():
                if sid and prefix:
                    pd_by_subscriber.setdefault(sid, f"{prefix}/{plen}")

        # Compute the full work list in memory while the dotmac session is
        # alive, then release it BEFORE the radius writes — holding the read
        # transaction through the write phase trips the app's 120s
        # idle-in-transaction timeout on large fleets.
        by_login: dict[str, tuple[str, str, list, bool, SubscriptionStatus, str]] = {}
        for sub in rows:
            login = cast(str | None, sub.login)
            if not login:
                stats["skipped_no_credential"] += 1
                continue
            cred = creds_by_username.get(login)
            if cred is None:
                stats["skipped_no_credential"] += 1
                continue
            if not cred.secret_hash:
                stats["skipped_no_password"] += 1
                continue
            try:
                cleartext = decrypt_credential_with_key(cred.secret_hash, enc_key)
            except Exception as exc:  # noqa: BLE001
                logger.warning("decrypt failed for %s: %s", login, exc)
                stats["skipped_decrypt_failed"] += 1
                continue
            if not cleartext:
                stats["skipped_no_password"] += 1
                continue

            sub_blocked = sub.subscriber_id in blocked_subscriber_ids
            captive = sub.subscriber_id in captive_optin_ids
            eff_ipv4 = sub.ipv4_address
            if not eff_ipv4 or eff_ipv4 == "0.0.0.0":  # nosec B104  # noqa: S104
                eff_ipv4 = ipv4_by_subscriber.get(sub.subscriber_id)
            attrs = _radreply_attrs(
                sub,
                sub.offer,
                sub.radius_profile,
                sub_blocked,
                captive_redirect_enabled=captive,
                additional_routes=routes_by_subscriber.get(sub.subscriber_id),
                framed_ipv4=eff_ipv4,
                delegated_ipv6=pd_by_subscriber.get(sub.subscriber_id),
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
            # Duplicate logins (migration duplicates): the ACTIVE sub wins the
            # slot — subscriber-level block still dominates via sub_blocked,
            # so a blocked customer stays enforced either way.
            existing = by_login.get(login)
            if existing is not None and existing[4] == SubscriptionStatus.active:
                continue
            by_login[login] = (
                login,
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
        print("\nTo execute: python -m app.services.radius_population --execute")


# ---------------------------------------------------------------------------
# Staff device-login RADIUS projection
# ---------------------------------------------------------------------------


def effective_roles(db, system_user_id) -> set[str]:
    """Return the set of active role names held by a SystemUser.

    Mirrors the SystemUser branch in auth_dependencies.has_permission:
    join system_user_roles → roles and collect Role.name where Role.is_active.
    """
    from app.models.rbac import Role, SystemUserRole

    rows = (
        db.query(Role.name)
        .join(SystemUserRole, SystemUserRole.role_id == Role.id)
        .filter(SystemUserRole.system_user_id == system_user_id)
        .filter(Role.is_active.is_(True))
        .all()
    )
    return {row[0] for row in rows}


def effective_perms(db, system_user_id) -> set[str]:
    """Return the set of active permission keys held by a SystemUser.

    Mirrors the SystemUser branch in auth_dependencies.has_permission:
    collects keys from (a) role-via-system_user_roles and (b) direct
    SystemUserPermission grants, both filtered by Permission.is_active.
    """
    from app.models.rbac import (
        Permission,
        Role,
        RolePermission,
        SystemUserPermission,
        SystemUserRole,
    )

    # (a) role-derived permissions
    role_perm_keys = (
        db.query(Permission.key)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .join(Role, RolePermission.role_id == Role.id)
        .join(SystemUserRole, SystemUserRole.role_id == Role.id)
        .filter(SystemUserRole.system_user_id == system_user_id)
        .filter(Role.is_active.is_(True))
        .filter(Permission.is_active.is_(True))
        .all()
    )

    # (b) direct grants
    direct_perm_keys = (
        db.query(Permission.key)
        .join(SystemUserPermission, SystemUserPermission.permission_id == Permission.id)
        .filter(SystemUserPermission.system_user_id == system_user_id)
        .filter(Permission.is_active.is_(True))
        .all()
    )

    return {row[0] for row in role_perm_keys} | {row[0] for row in direct_perm_keys}


def populate_device_login(
    db,
    *,
    dry_run: bool = False,
    _conn_factory=None,
) -> dict[str, int]:
    """Project device-login-enabled staff into the admin RADIUS auth set.

    Writes ONLY to radcheck_admin / radreply_admin — never touches radcheck /
    radreply (subscriber auth).

    Per-user DELETE+INSERT pattern (idempotent): same as populate().  Eligible
    users get:
      radcheck_admin:  Cleartext-Password := <decrypted secret>
      radreply_admin:  Mikrotik-Group := <tier>
                       Service-Type   := Administrative-User

    Args:
        db: SQLAlchemy session (dotmac_sub app DB — SystemUser side).
        dry_run: If True, compute stats and issue SQL but rollback; no writes.
        _conn_factory: Optional zero-arg callable that returns a DB-API 2
            connection to the RADIUS DB.  Used by tests to inject an in-memory
            SQLite connection in place of the real psycopg Postgres connection.
            When None (production), connects to the resolved radius DSN
            (radius_dsn.radius_dsn_libpq()).

    Returns:
        dict with keys: considered, radcheck_upserts, radreply_upserts,
        removed, skipped_ineligible.
    """
    from app.models.system_user import SystemUser
    from app.services.credential_crypto import decrypt_credential
    from app.services.device_login import derive_router_tier

    stats: dict[str, int] = {
        "considered": 0,
        "radcheck_upserts": 0,
        "radreply_upserts": 0,
        "removed": 0,
        "skipped_ineligible": 0,
    }

    # Fetch all active SystemUsers from the app DB while we still hold the
    # session; compute work list in memory before opening the RADIUS connection
    # (avoids holding the app transaction open during RADIUS writes).
    staff = db.query(SystemUser).filter(SystemUser.is_active.is_(True)).all()

    # Build work list: (username, cleartext|None, tier|None, eligible)
    # tier=None + eligible=True  → skipped_ineligible
    # tier=None + eligible=False → removed
    # tier set               → upsert
    work: list[tuple[str, str | None, str | None, bool]] = []
    for u in staff:
        stats["considered"] += 1
        eligible = bool(
            u.device_login_enabled
            and u.device_login_revoked_at is None
            and u.device_login_secret
        )
        if not eligible:
            work.append((u.email, None, None, False))
            continue

        roles = effective_roles(db, u.id)
        perms = effective_perms(db, u.id)
        tier = derive_router_tier(roles, perms)

        if tier is None:
            work.append((u.email, None, None, True))
            continue

        try:
            cleartext = decrypt_credential(u.device_login_secret)
        except Exception:  # noqa: BLE001
            logger.warning(
                "populate_device_login: decrypt failed for %s — skipping", u.email
            )
            work.append((u.email, None, None, True))
            continue

        if not cleartext:
            work.append((u.email, None, None, True))
            continue

        work.append((u.email, cleartext, tier, True))

    # Open RADIUS connection
    if _conn_factory is not None:
        conn = _conn_factory()
    else:
        radius_dsn = radius_dsn_libpq()
        if not radius_dsn:
            raise RuntimeError("RADIUS database DSN not configured")
        conn = psycopg.connect(radius_dsn)
        conn.autocommit = False

    try:
        cur = conn.cursor()
        for uname, cleartext, tier, ineligible_flag in work:
            # Always clean old rows first — idempotent regardless of outcome.
            cur.execute("DELETE FROM radcheck_admin WHERE username=%s", (uname,))
            cur.execute("DELETE FROM radreply_admin WHERE username=%s", (uname,))

            if tier is None:
                if ineligible_flag:
                    # Also counts enabled-but-unusable users (decrypt failure / empty secret), not only permission-ineligible ones
                    stats["skipped_ineligible"] += 1
                else:
                    stats["removed"] += 1
                continue

            # Upsert
            cur.execute(
                "INSERT INTO radcheck_admin (username, attribute, op, value) "
                "VALUES (%s, 'Cleartext-Password', ':=', %s)",
                (uname, cleartext),
            )
            cur.execute(
                "INSERT INTO radreply_admin (username, attribute, op, value) "
                "VALUES (%s, 'Mikrotik-Group', ':=', %s)",
                (uname, tier),
            )
            cur.execute(
                "INSERT INTO radreply_admin (username, attribute, op, value) "
                "VALUES (%s, 'Service-Type', ':=', 'Administrative-User')",
                (uname,),
            )
            stats["radcheck_upserts"] += 1
            stats["radreply_upserts"] += 2

        if dry_run:
            conn.rollback()
        else:
            conn.commit()
    finally:
        conn.close()

    logger.info("populate_device_login done (dry_run=%s): %s", dry_run, stats)
    return stats


# -----------------------------------------------------------------------------
# TODO — Celery change-driven handler
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
