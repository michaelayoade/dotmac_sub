"""Celery tasks for RADIUS population from local authoritative state."""

from __future__ import annotations

import logging

from app.celery_app import celery_app

logger = logging.getLogger(__name__)


# Single-flight advisory-lock key for the RADIUS populate sweep. Two overlapping
# refreshes (event-enqueued + the 15-min safety net) used to interleave their
# per-user DELETE+INSERT and orphan-cleanup, leaving a transient window where a
# live user had no radcheck row (access-reject → session drop). This serializes
# them: a second run that can't take the lock skips rather than fighting.
_POPULATE_LOCK_KEY = 778_001


@celery_app.task(name="app.tasks.radius_population.refresh_radius_from_subs")
def refresh_radius_from_subs() -> dict[str, int]:
    """Rebuild radcheck + radreply from dotmac_sub authoritative joins."""
    from sqlalchemy import func, select

    from app.db import SessionLocal
    from app.services.radius_population import populate

    logger.info("RADIUS refresh-from-subs starting")
    lock_db = SessionLocal()
    try:
        bind = lock_db.bind
        is_pg = bind is not None and bind.dialect.name == "postgresql"
        if is_pg:
            acquired = lock_db.execute(
                select(func.pg_try_advisory_lock(_POPULATE_LOCK_KEY))
            ).scalar()
            # Commit so the lock-holding connection isn't left "idle in
            # transaction" (session-level advisory locks survive commit).
            lock_db.commit()
            if not acquired:
                logger.info(
                    "RADIUS refresh-from-subs: another run holds the lock; skipping"
                )
                return {"skipped_locked": 1}
        try:
            result = populate(dry_run=False)
        finally:
            if is_pg:
                lock_db.execute(select(func.pg_advisory_unlock(_POPULATE_LOCK_KEY)))
                lock_db.commit()
    finally:
        lock_db.close()
    logger.info("RADIUS refresh-from-subs complete: %s", result)
    return result
