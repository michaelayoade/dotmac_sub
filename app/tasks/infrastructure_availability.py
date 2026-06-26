"""Infrastructure availability snapshot Celery tasks — daily trend capture.

Mirrors ``app.tasks.ip_utilization``: one task snapshots yesterday's
availability per element, a sibling prunes beyond the retention window. Wire
both into Celery beat. See INFRASTRUCTURE_SLA_PERFORMANCE.md Phase 2.
"""

import logging

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)


@celery_app.task(
    name="app.tasks.infrastructure_availability.snapshot_infrastructure_availability"
)
def snapshot_infrastructure_availability() -> dict:
    """Capture yesterday's availability snapshot for every element."""
    logger.info("Starting infrastructure availability snapshot task")
    with db_session_adapter.session() as db:
        from app.services import infrastructure_availability_snapshot as svc

        result = svc.take_snapshot(db)
        logger.info(
            "Infrastructure availability snapshot complete: rows=%d day=%s",
            result["created"],
            result["day"],
        )
        return result


@celery_app.task(
    name="app.tasks.infrastructure_availability.prune_infrastructure_availability"
)
def prune_infrastructure_availability() -> dict:
    """Delete availability snapshots beyond the retention window."""
    logger.info("Starting infrastructure availability snapshot prune")
    with db_session_adapter.session() as db:
        from app.services import infrastructure_availability_snapshot as svc

        result = svc.prune(db)
        logger.info(
            "Infrastructure availability snapshot prune complete: deleted=%d",
            result["deleted"],
        )
        return result
