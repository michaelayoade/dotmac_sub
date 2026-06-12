import logging

from app.celery_app import celery_app
from app.services import radius as radius_service
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)
SessionLocal = db_session_adapter.create_session


@celery_app.task(name="app.tasks.radius.run_radius_sync_job")
def run_radius_sync_job(job_id: str) -> dict[str, int]:
    logger.info("Starting run_radius_sync_job for job_id=%s", job_id)
    session = SessionLocal()
    try:
        radius_service.radius_sync_jobs.run(session, job_id)
        logger.info("Completed run_radius_sync_job for job_id=%s", job_id)
        session.commit()
        return {"processed": 1, "errors": 0}
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@celery_app.task(name="app.tasks.radius.audit_suspension_enforcement")
def audit_suspension_enforcement() -> dict:
    """Periodic read-only check that fully-blocked subscribers are actually
    unenforced in the external RADIUS DB (no reject, no walled-garden).
    Logs a warning per leak class and stores the result in Redis, where the
    web process's metrics collector exports it as
    radius_suspension_audit_leaks{kind} — drift here used to accumulate
    invisibly (suspended subscribers staying online)."""
    from app.services.radius_reconciliation import (
        audit_suspension_enforcement as run_audit,
    )
    from app.services.radius_reconciliation import store_latest_audit

    session = SessionLocal()
    try:
        result = run_audit(session)
        store_latest_audit(result)
        return result
    finally:
        session.close()


@celery_app.task(name="app.tasks.radius.run_enforcement_reconciler")
def run_enforcement_reconciler() -> dict[str, int]:
    """Assert that non-serviceable subscribers are actually unreachable.

    Closes the gap between billing-state changes (which only affect the
    next re-auth) and live PPPoE sessions (incidents 2026-06-11:
    100009689, 100025880):

    1. Open radacct sessions whose username has no radcheck row and that
       started >20 min ago -> CoA-kick (the redial is then cleanly
       rejected or walled-gardened by current state).
    2. Open sessions whose framed IP sits in a dotmac reject pool
       (blocked/negative/bad_mac/bad_password networks; the not_found
       pool is excluded because legit BNG-local pools overlap it) ->
       CoA-kick so the redial picks up a routable IP.
    3. Walled-garden drift: subscribers who must carry the suspended
       address-list but whose radreply lacks it -> enqueue the
       single-writer refresh.

    Kicks are capped per run so systemic drift degrades to alerts, not a
    mass disconnect.
    """
    import ipaddress
    import os

    import psycopg

    from app.db import SessionLocal
    from app.services.enforcement import _nas_device_by_ip, _send_coa_disconnect
    from app.services.radius_reject import get_reject_networks

    max_kicks = int(os.environ.get("ENFORCEMENT_RECONCILER_MAX_KICKS", "25"))
    dsn = os.environ.get("RADIUS_DB_DSN", "")
    stats = {
        "stale_unserviceable_sessions": 0,
        "reject_pool_sessions": 0,
        "kicked": 0,
        "kick_failed": 0,
        "kicks_capped": 0,
        "walled_garden_drift": 0,
        "sync_gap_logins": 0,
        "ghosts_closed": 0,
    }
    if not dsn:
        logger.error("enforcement reconciler: RADIUS_DB_DSN not set")
        return stats

    # --- collect violations from radacct -------------------------------
    with psycopg.connect(dsn) as rconn, rconn.cursor() as cur:
        cur.execute(
            "SELECT r.username, r.acctsessionid, host(r.nasipaddress), "
            "host(r.framedipaddress), r.radacctid, "
            "GREATEST(r.acctstarttime, COALESCE(r.acctupdatetime, "
            "r.acctstarttime)) < now() - interval '2 hours' AS stale "
            "FROM radacct r "
            "WHERE r.acctstoptime IS NULL "
            "AND r.acctstarttime < now() - interval '20 minutes' "
            "AND r.username IS NOT NULL AND r.username <> '' "
            "AND NOT EXISTS (SELECT 1 FROM radcheck rc "
            "                WHERE rc.username = r.username)",
        )
        unserviceable = cur.fetchall()
        cur.execute(
            "SELECT r.username, r.acctsessionid, host(r.nasipaddress), "
            "host(r.framedipaddress), r.radacctid, false AS stale "
            "FROM radacct r "
            "WHERE r.acctstoptime IS NULL AND r.framedipaddress IS NOT NULL",
        )
        open_sessions = cur.fetchall()

    stats["stale_unserviceable_sessions"] = len(unserviceable)
    to_kick = {(row[1], row[2]): row for row in unserviceable}

    db = SessionLocal()
    try:
        reject_nets = {
            reason: net
            for reason, net in get_reject_networks(db).items()
            if reason != "not_found"
        }
        for row in open_sessions:
            framed = row[3]
            if not framed:
                continue
            try:
                addr = ipaddress.ip_address(framed)
            except ValueError:
                continue
            if any(addr in net for net in reject_nets.values()):
                if (row[1], row[2]) not in to_kick:
                    stats["reject_pool_sessions"] += 1
                    to_kick[(row[1], row[2])] = row

        # Dual-run guard: never kick a login that is ACTIVE in Splynx.
        # Sync gaps exist where a Splynx-active service has no dotmac
        # subscription yet (e.g. 100025599 on 2026-06-11) — those users
        # are missing from radcheck through no fault of their own, and
        # kicking them is an outage for a paying customer. Surface them
        # as sync-gap alerts instead.
        kick_logins = sorted({row[0] for row in to_kick.values()})
        splynx_active: set[str] = set()
        if kick_logins:
            try:
                from scripts.migration.db_connections import splynx_connection

                with splynx_connection() as sconn, sconn.cursor() as scur:
                    placeholders = ",".join(["%s"] * len(kick_logins))
                    scur.execute(
                        "SELECT login FROM services_internet "  # nosec B608  # noqa: S608 — %s binds; values passed as params
                        f"WHERE login IN ({placeholders}) "
                        "AND deleted='0' AND status='active'",
                        kick_logins,
                    )
                    for r in scur.fetchall():
                        login = r["login"] if isinstance(r, dict) else r[0]
                        splynx_active.add(str(login).strip())
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "enforcement reconciler: Splynx guard query failed (%s) — "
                    "skipping ALL kicks this run (fail-safe)",
                    exc,
                )
                splynx_active = {row[0] for row in to_kick.values()}
        if splynx_active:
            stats["sync_gap_logins"] = len(splynx_active)
            logger.error(
                "enforcement reconciler: %d logins are ACTIVE in Splynx but "
                "missing from radcheck — dotmac sync gap, NOT kicking: %s",
                len(splynx_active),
                sorted(splynx_active)[:10],
            )

        ghost_rows: list[tuple[int, str]] = []
        for (
            username,
            session_id,
            nas_ip,
            framed_ip,
            radacctid,
            stale,
        ) in to_kick.values():
            if username in splynx_active:
                continue
            if stats["kicked"] >= max_kicks:
                stats["kicks_capped"] = len(to_kick) - stats["kicked"]
                logger.error(
                    "enforcement reconciler: kick cap (%d) reached with %d "
                    "violations outstanding — investigate systemic drift",
                    max_kicks,
                    stats["kicks_capped"],
                )
                break
            nas_device = _nas_device_by_ip(db, nas_ip)
            if not nas_device:
                stats["kick_failed"] += 1
                logger.warning(
                    "enforcement reconciler: no NasDevice for NAS %s "
                    "(user %s, session %s)",
                    nas_ip,
                    username,
                    session_id,
                )
                continue
            if _send_coa_disconnect(db, nas_device, username, framed_ip, session_id):
                stats["kicked"] += 1
                logger.info(
                    "enforcement reconciler: kicked %s on %s (session %s, ip %s)",
                    username,
                    nas_ip,
                    session_id,
                    framed_ip,
                )
            elif stale:
                # CoA failed AND the row hasn't seen accounting in >2h
                # (interim cadence is 5 min): the session no longer exists
                # on the NAS — a ghost row from a lost Stop packet. The
                # BNGs Disconnect-NAK these (code 42). Close the row so it
                # stops masquerading as an enforcement leak.
                ghost_rows.append((radacctid, username))
            else:
                stats["kick_failed"] += 1

        if ghost_rows:
            with psycopg.connect(dsn) as rconn, rconn.cursor() as cur:
                cur.execute(
                    "UPDATE radacct SET acctstoptime = now(), "
                    "acctterminatecause = 'Ghost-Reconciled' "
                    "WHERE radacctid = ANY(%s) AND acctstoptime IS NULL",
                    ([rid for rid, _ in ghost_rows],),
                )
                rconn.commit()
            stats["ghosts_closed"] = len(ghost_rows)
            logger.info(
                "enforcement reconciler: closed %d ghost radacct rows "
                "(no accounting >2h, NAS refused disconnect): %s",
                len(ghost_rows),
                sorted({u for _, u in ghost_rows})[:10],
            )

        # --- walled-garden drift check ---------------------------------
        from sqlalchemy import select

        from app.models.catalog import Subscription, SubscriptionStatus
        from app.models.subscriber import Subscriber, SubscriberStatus

        blocked_subscriber_ids = {
            sid
            for (sid,) in db.execute(
                select(Subscriber.id).where(
                    Subscriber.status == SubscriberStatus.blocked
                )
            ).all()
        }
        # Mirror populate_radius_from_subs's per-login slot policy: the
        # ACTIVE sub wins a shared login, so the tag is expected only if
        # the winner's subscriber is blocked — or if the login has no
        # active sub at all (then any blocked/suspended sub carries the
        # tag). Without this, mixed-status logins (e.g. 100025926, active
        # plan + suspended add-on) false-positive.
        per_login: dict[str, dict] = {}
        for login, sub_status, subscriber_id in db.execute(
            select(
                Subscription.login,
                Subscription.status,
                Subscription.subscriber_id,
            ).where(
                Subscription.status.in_(
                    [
                        SubscriptionStatus.active,
                        SubscriptionStatus.blocked,
                        SubscriptionStatus.suspended,
                    ]
                ),
                Subscription.login.isnot(None),
            )
        ).all():
            info = per_login.setdefault(
                login, {"has_active": False, "active_blocked": False}
            )
            if sub_status == SubscriptionStatus.active:
                info["has_active"] = True
                if subscriber_id in blocked_subscriber_ids:
                    info["active_blocked"] = True
        expected_wg = {
            login
            for login, info in per_login.items()
            if (info["has_active"] and info["active_blocked"]) or not info["has_active"]
        }
    finally:
        db.close()

    if expected_wg:
        with psycopg.connect(dsn) as rconn, rconn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT username FROM radreply "
                "WHERE attribute='Mikrotik-Address-List' AND value='suspended'",
            )
            tagged = {r[0] for r in cur.fetchall()}
            cur.execute("SELECT DISTINCT username FROM radcheck")
            in_radcheck = {r[0] for r in cur.fetchall()}
        # only logins that are supposed to be in radcheck can drift
        drift = (expected_wg & in_radcheck) - tagged
        stats["walled_garden_drift"] = len(drift)
        if drift:
            logger.error(
                "enforcement reconciler: %d walled-garden users missing the "
                "suspended tag (sample: %s) — enqueueing refresh",
                len(drift),
                sorted(drift)[:5],
            )
            from app.tasks.splynx_sync import run_refresh_radius_from_subs

            run_refresh_radius_from_subs.delay()

    logger.info("enforcement reconciler done: %s", stats)
    return stats
