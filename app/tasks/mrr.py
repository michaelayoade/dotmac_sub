"""MRR snapshot Celery task — nightly revenue snapshot."""

import logging

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.mrr.snapshot_mrr")
def snapshot_mrr() -> dict[str, int]:
    """Take a daily MRR snapshot for all active subscribers.

    Designed to run nightly via Celery beat.
    """
    logger.info("Starting MRR snapshot task")
    with db_session_adapter.session() as db:
        from app.services.mrr_snapshot import mrr_snapshots

        result = mrr_snapshots.take_snapshot(db)
        logger.info(
            "MRR snapshot complete: created=%d updated=%d skipped=%d",
            result["created"],
            result["updated"],
            result["skipped"],
        )
        return result
