import logging

from app.celery_app import celery_app
from app.services import radius as radius_service
from app.services.db_session_adapter import db_session_adapter
from app.tasks._postgres_lock import postgres_session_advisory_lock

logger = logging.getLogger(__name__)
SessionLocal = db_session_adapter.create_session


@celery_app.task(name="app.tasks.radius.reap_radacct_ghosts")
def reap_radacct_ghosts() -> dict:
    """Close stale-open radacct sessions (dead NAS / lost Acct-Stop) so phantom
    'online' sessions don't accumulate. Age-based; safe (interim keeps live
    sessions fresh). Scheduled via scheduler_config; gated on the same
    radius_session_reap flag as the app-side reaper."""
    from app.services.radius_reconciliation import reap_stale_radacct_ghosts

    session = SessionLocal()
    try:
        return reap_stale_radacct_ghosts(session)
    finally:
        session.close()


@celery_app.task(name="app.tasks.radius.reconcile_active_sessions")
def reconcile_active_sessions(window_seconds: int | None = None) -> dict:
    """Rebuild the live ``radius_active_sessions`` view from OPEN external
    radacct sessions (username->login->subscriber, nasip->nas_device), upserting
    open sessions and pruning ended ones. Discover-reconcile, so it self-heals
    even though the FreeRADIUS accounting hook that was meant to populate the
    table is not firing in prod. Read-only against the external radius DB.

    Single-flight via an advisory lock so overlapping beats don't double-run."""
    from app.services.radius_session_reconcile import (
        ADVISORY_LOCK_KEY,
        reconcile_active_sessions_from_radacct,
    )

    with postgres_session_advisory_lock(ADVISORY_LOCK_KEY) as acquired:
        if not acquired:
            logger.info(
                "reconcile_active_sessions skipped: previous run still in progress"
            )
            return {"skipped": "already_running"}
        session = SessionLocal()
        try:
            result = reconcile_active_sessions_from_radacct(
                session, window_seconds=window_seconds
            )
            session.commit()
            return result
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


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


@celery_app.task(name="app.tasks.radius.audit_ip_consistency")
def audit_ip_consistency() -> dict:
    """Periodic read-only check that an active subscriber's IPv4 agrees across
    its three sources (subscription.ipv4_address column, the IPAM IPAssignment,
    and the external radreply Framed-IP). Drift here is the structural risk
    behind silent partial desync — see
    docs/designs/SERVICE_LIFECYCLE_BUNDLE_INTEGRITY.md. Stores the result in
    Redis where the web process's metrics collector exports it as
    radius_ip_consistency_drift{kind}."""
    from app.services.ip_consistency_audit import (
        audit_ip_consistency as run_audit,
    )
    from app.services.ip_consistency_audit import store_latest_ip_audit

    session = SessionLocal()
    try:
        result = run_audit(session)
        store_latest_ip_audit(result)
        return result
    finally:
        session.close()


@celery_app.task(name="app.tasks.radius.connectivity_shadow_audit")
def connectivity_shadow_audit() -> dict:
    """Periodic read-only full-base connectivity shadow sweep. Aggregates
    per-dimension desired-vs-actual drift across every connectivity-retaining
    subscriber and stores it in Redis, where the web process's metrics collector
    exports it as ``connectivity_shadow_drift{dimension}``. This is the
    cutover-readiness gauge for the connectivity reconciler — see
    docs/designs/CONNECTIVITY_STATE_MACHINE.md. Writes nothing."""
    from app.services.connectivity_reconciler import (
        connectivity_shadow_audit as run_sweep,
    )
    from app.services.connectivity_reconciler import (
        store_connectivity_shadow_result,
    )

    session = SessionLocal()
    try:
        result = run_sweep(session)
        store_connectivity_shadow_result(result)
        logger.info(
            "connectivity shadow audit: population=%s drift=%s",
            result.get("population"),
            result.get("counts"),
        )
        return result
    finally:
        session.close()


@celery_app.task(name="app.tasks.radius.run_enforcement_reconciler")
def run_enforcement_reconciler() -> dict[str, int]:
    """Converge local lifecycle state, RADIUS projection, and live sessions.

    This is the one periodic recovery loop for account access. Immediate
    payment/lifecycle events use the same owners; this task repairs a missed
    event without inventing a second status or walled-garden policy.

    1. Re-derive blocking parent/account projections from active child service
       facts when there is no explicit lifecycle override.
    2. Open radacct sessions whose username has no radcheck row and that
       started >20 min ago -> CoA-kick (the redial is then cleanly
       rejected or walled-gardened by current state).
    3. Open sessions whose framed IP sits in a dotmac reject pool
       (blocked/negative/bad_mac/bad_password networks; the not_found
       pool is excluded because legit BNG-local pools overlap it) ->
       CoA-kick so the redial picks up a routable IP.
    4. Compare canonical per-login projection modes against radcheck/radreply
       in both directions and request one single-writer refresh for any drift.

    Kicks are capped per run so systemic drift degrades to alerts, not a
    mass disconnect.
    """
    import ipaddress

    import psycopg
    from psycopg import sql

    from app.db import SessionLocal
    from app.models.domain_settings import SettingDomain
    from app.services import settings_spec
    from app.services.enforcement import _nas_device_by_ip, _send_coa_disconnect
    from app.services.external_radius_targets import authoritative_accounting_target
    from app.services.radius_address_lists import suspended_address_list
    from app.services.radius_reject import get_reject_networks

    stats = {
        "account_projection_candidates": 0,
        "account_projections_changed": 0,
        "access_states_changed": 0,
        "account_projection_errors": 0,
        "accounting_target_configured": 0,
        "stale_unserviceable_sessions": 0,
        "reject_pool_sessions": 0,
        "kicked": 0,
        "kick_failed": 0,
        "kicks_capped": 0,
        "walled_garden_drift": 0,
        "missing_radius_auth": 0,
        "missing_reject": 0,
        "stale_reject": 0,
        "missing_captive": 0,
        "stale_captive": 0,
        "radius_projection_unconverged": 0,
        "radius_projection_repair_enqueued": 0,
        "sync_gap_logins": 0,
        "ghosts_closed": 0,
    }
    db = SessionLocal()
    from app.services.account_status_reconcile import reconcile_cohort

    local = reconcile_cohort(
        db,
        dry_run=False,
        refresh_radius=False,
        send_coa=False,
        notify=False,
    )
    stats["account_projection_candidates"] = local.candidates
    stats["account_projections_changed"] = local.changed
    stats["access_states_changed"] = local.access_states_changed
    stats["account_projection_errors"] = local.errors

    from app.services.radius_projection_planner import (
        compare_radius_projection,
        plan_login_radius_projections,
    )

    desired_login_projections = plan_login_radius_projections(db)
    desired_modes = {
        login: projection.plan.mode
        for login, projection in desired_login_projections.items()
    }
    max_kicks = int(
        settings_spec.resolve_value(
            db, SettingDomain.radius, "enforcement_reconciler_max_kicks"
        )
        or 25
    )
    unserviceable_grace_seconds = int(
        settings_spec.resolve_value(
            db,
            SettingDomain.radius,
            "enforcement_reconciler_unserviceable_grace_seconds",
        )
        or 1200
    )
    ghost_stale_seconds = int(
        settings_spec.resolve_value(
            db,
            SettingDomain.radius,
            "enforcement_reconciler_ghost_stale_seconds",
        )
        or 7200
    )
    walled_garden_address_list = suspended_address_list(db)
    target = authoritative_accounting_target(db)
    db.rollback()
    if not target:
        db.close()
        logger.error("enforcement reconciler: accounting target not configured")
        from app.services.observability import record_task_run

        record_task_run(
            "app.tasks.radius.run_enforcement_reconciler",
            status="degraded",
            counters=stats,
        )
        return stats
    stats["accounting_target_configured"] = 1
    dsn = str(target["db_url"]).replace("postgresql+psycopg://", "postgresql://", 1)
    radacct = sql.Identifier(*str(target["radacct_table"]).split("."))
    radcheck = sql.Identifier(*str(target["radcheck_table"]).split("."))
    radreply = sql.Identifier(*str(target["radreply_table"]).split("."))
    radius_refresh_required = bool(local.changed or local.access_states_changed)

    # --- collect violations from radacct -------------------------------
    with psycopg.connect(dsn) as rconn, rconn.cursor() as cur:
        cur.execute(
            sql.SQL(
                "SELECT r.username, r.acctsessionid, host(r.nasipaddress), "
                "host(r.framedipaddress), r.radacctid, "
                "GREATEST(r.acctstarttime, COALESCE(r.acctupdatetime, "
                "r.acctstarttime)) < now() - (%s * interval '1 second') AS stale "
                "FROM {} r "
                "WHERE r.acctstoptime IS NULL "
                "AND r.acctstarttime < now() - (%s * interval '1 second') "
                "AND r.username IS NOT NULL AND r.username <> '' "
                "AND NOT EXISTS (SELECT 1 FROM {} rc "
                "                WHERE rc.username = r.username)"
            ).format(radacct, radcheck),
            (ghost_stale_seconds, unserviceable_grace_seconds),
        )
        unserviceable = cur.fetchall()
        cur.execute(
            sql.SQL(
                "SELECT r.username, r.acctsessionid, host(r.nasipaddress), "
                "host(r.framedipaddress), r.radacctid, false AS stale "
                "FROM {} r "
                "WHERE r.acctstoptime IS NULL AND r.framedipaddress IS NOT NULL"
            ).format(radacct)
        )
        open_sessions = cur.fetchall()

    stats["stale_unserviceable_sessions"] = len(unserviceable)
    to_kick = {(row[1], row[2]): row for row in unserviceable}

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

        # Sync-gap guard: a missing radcheck row is an observation of
        # projection drift, not an access decision. The external sync skips
        # credentials it cannot rebuild a password for, so an entitled
        # customer (active subscription, non-blocked active subscriber) can
        # hold a live session with no radcheck row — kicking them drops a
        # session they cannot re-establish. Spare those sessions, surface
        # the gap, and enqueue the single-writer refresh to repair the
        # projection. Reject-pool violations are still kicked: there the
        # radcheck row exists and a re-auth restores real service.
        gap_candidates = {row[0] for row in unserviceable}
        if gap_candidates:
            entitled = {
                login
                for login in gap_candidates
                if desired_modes.get(login) in {"active", "captive"}
            }
            if entitled:
                unserviceable_keys = {(row[1], row[2]) for row in unserviceable}
                to_kick = {
                    key: row
                    for key, row in to_kick.items()
                    if not (key in unserviceable_keys and row[0] in entitled)
                }
                stats["sync_gap_logins"] = len(entitled)
                logger.error(
                    "enforcement reconciler: %d entitled logins have live "
                    "sessions but no radcheck row (sync gap) — sparing them "
                    "and enqueueing refresh (sample: %s)",
                    len(entitled),
                    sorted(entitled)[:5],
                )
                radius_refresh_required = True

        ghost_rows: list[tuple[int, str]] = []
        for (
            username,
            session_id,
            nas_ip,
            framed_ip,
            radacctid,
            stale,
        ) in to_kick.values():
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
                    sql.SQL(
                        "UPDATE {} SET acctstoptime = now(), "
                        "acctterminatecause = 'Ghost-Reconciled' "
                        "WHERE radacctid = ANY(%s) AND acctstoptime IS NULL"
                    ).format(radacct),
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

    finally:
        db.close()

    # --- canonical desired-vs-observed projection ---------------------
    with psycopg.connect(dsn) as rconn, rconn.cursor() as cur:
        cur.execute(sql.SQL("SELECT DISTINCT username FROM {}").format(radcheck))
        in_radcheck = {r[0] for r in cur.fetchall()}
        cur.execute(
            sql.SQL(
                "SELECT DISTINCT username FROM {} "
                "WHERE lower(attribute)='auth-type' AND lower(value)='reject'"
            ).format(radcheck)
        )
        rejected = {r[0] for r in cur.fetchall()}
        cur.execute(
            sql.SQL(
                "SELECT DISTINCT username FROM {} "
                "WHERE attribute='Mikrotik-Address-List' AND value=%s"
            ).format(radreply),
            (walled_garden_address_list,),
        )
        captive_tagged = {r[0] for r in cur.fetchall()}

    drift = compare_radius_projection(
        desired_login_projections,
        observed_auth=in_radcheck,
        observed_reject=rejected,
        observed_captive=captive_tagged,
    )
    stats["missing_radius_auth"] = len(drift.missing_auth)
    stats["missing_reject"] = len(drift.missing_reject)
    stats["stale_reject"] = len(drift.stale_reject)
    stats["missing_captive"] = len(drift.missing_captive)
    stats["stale_captive"] = len(drift.stale_captive)
    stats["walled_garden_drift"] = len(drift.missing_captive | drift.stale_captive)
    stats["radius_projection_unconverged"] = len(drift.usernames)
    radius_refresh_required = radius_refresh_required or bool(drift.usernames)

    if drift.usernames:
        logger.error(
            "access projection unconverged: total=%d missing_auth=%d "
            "missing_reject=%d stale_reject=%d missing_captive=%d "
            "stale_captive=%d sample=%s",
            len(drift.usernames),
            len(drift.missing_auth),
            len(drift.missing_reject),
            len(drift.stale_reject),
            len(drift.missing_captive),
            len(drift.stale_captive),
            sorted(drift.usernames)[:5],
        )

    if radius_refresh_required:
        from app.tasks.radius_population import refresh_radius_from_subs

        refresh_radius_from_subs.delay()
        stats["radius_projection_repair_enqueued"] = 1

    from app.services.observability import record_task_run

    record_task_run(
        "app.tasks.radius.run_enforcement_reconciler",
        status=(
            "degraded"
            if stats["account_projection_errors"]
            or stats["radius_projection_unconverged"]
            else "ok"
        ),
        counters=stats,
    )

    logger.info("enforcement reconciler done: %s", stats)
    return stats
