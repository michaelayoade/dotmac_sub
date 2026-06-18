"""Celery tasks for RADIUS population from local authoritative state."""

from __future__ import annotations

import logging

from app.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.radius_population.refresh_radius_from_subs")
def refresh_radius_from_subs() -> dict[str, int]:
    """Rebuild radcheck + radreply from dotmac_sub authoritative joins."""
    from app.services.radius_population import populate

    logger.info("RADIUS refresh-from-subs starting")
    result = populate(dry_run=False)
    logger.info("RADIUS refresh-from-subs complete: %s", result)
    return result
