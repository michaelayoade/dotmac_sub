"""IP pool utilization snapshot Celery task — periodic capture for trends."""

import logging

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.ip_utilization.snapshot_ip_pool_utilization")
def snapshot_ip_pool_utilization() -> dict[str, int]:
    """Capture a utilization snapshot for every active IP pool.

    Designed to run periodically via Celery beat so the admin pool detail can
    chart usage over time.
    """
    logger.info("Starting IP pool utilization snapshot task")
    with db_session_adapter.session() as db:
        from app.services.ip_pool_utilization_snapshot import (
            ip_pool_utilization_snapshots,
        )

        result = ip_pool_utilization_snapshots.take_snapshot(db)
        logger.info(
            "IP pool utilization snapshot complete: pools=%d", result["created"]
        )
        return result
