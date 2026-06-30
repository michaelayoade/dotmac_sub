"""Celery tasks for the local self-serve quote mirror."""

import logging

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.quotes.reconcile_quote_mirror")
def reconcile_quote_mirror() -> dict[str, int]:
    """Reconcile stale local quote mirrors against the CRM (backstop for missed
    webhook deliveries). Returns {reconciled: N}."""
    logger.info("Starting reconcile_quote_mirror")
    db = db_session_adapter.create_session()
    try:
        from app.services import quotes_mirror

        count = quotes_mirror.reconcile_all(db)
        logger.info("Completed reconcile_quote_mirror: reconciled=%s", count)
        return {"reconciled": count}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
