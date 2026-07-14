"""Celery tasks for RADIUS population from local authoritative state."""

from __future__ import annotations

import logging

from app.celery_app import celery_app
from app.tasks._postgres_lock import postgres_session_advisory_lock

logger = logging.getLogger(__name__)


# Single-flight advisory-lock key for the RADIUS populate sweep. Two overlapping
# refreshes (event-enqueued + the 15-min safety net) used to interleave their
# per-user DELETE+INSERT and orphan-cleanup, leaving a transient window where a
# live user had no radcheck row (access-reject → session drop). This serializes
# them: a second run that can't take the lock skips rather than fighting.
# Locks go through postgres_session_advisory_lock (pinned connection): a pooled
# Session that commits after acquiring can unlock on a different backend, which
# strands the lock and skips every later run.
_POPULATE_LOCK_KEY = 778_001


@celery_app.task(name="app.tasks.radius_population.refresh_radius_from_subs")
def refresh_radius_from_subs() -> dict[str, int]:
    """Rebuild radcheck + radreply from dotmac_sub authoritative joins."""
    from app.services.radius_population import populate

    logger.info("RADIUS refresh-from-subs starting")
    with postgres_session_advisory_lock(_POPULATE_LOCK_KEY) as acquired:
        if not acquired:
            logger.info(
                "RADIUS refresh-from-subs: another run holds the lock; skipping"
            )
            return {"skipped_locked": 1}
        result = populate(dry_run=False)
    logger.info("RADIUS refresh-from-subs complete: %s", result)
    return result


# ---------------------------------------------------------------------------
# Staff device-login sync task
# ---------------------------------------------------------------------------

_DEVICE_LOGIN_LOCK_KEY = 778_002


@celery_app.task(name="app.tasks.radius_population.sync_device_login")
def sync_device_login() -> dict[str, int]:
    """Rebuild radcheck_admin + radreply_admin from active SystemUser device-login state.

    Runs the same advisory-lock guard as refresh_radius_from_subs so a
    concurrently-enqueued sync doesn't race the periodic one.
    """
    from app.db import SessionLocal
    from app.services.radius_population import (
        populate_device_login,
        record_device_login_sync_status,
    )

    logger.info("RADIUS device-login sync starting")
    with postgres_session_advisory_lock(_DEVICE_LOGIN_LOCK_KEY) as acquired:
        if not acquired:
            logger.info(
                "RADIUS device-login sync: another run holds the lock; skipping"
            )
            return {"skipped_locked": 1}
        db = SessionLocal()
        try:
            try:
                result = populate_device_login(db, dry_run=False)
                record_device_login_sync_status(db, status="ok", result=result)
            except Exception as exc:
                db.rollback()
                record_device_login_sync_status(db, status="failed", error=str(exc))
                raise
        finally:
            db.close()
    logger.info("RADIUS device-login sync complete: %s", result)
    return result
