"""Celery tasks for the local work-order/field-service mirror."""

import logging

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.work_orders.reconcile_work_order_mirror")
def reconcile_work_order_mirror() -> dict[str, int]:
    """Reconcile stale local work-order mirrors against the CRM (backstop for
    missed webhook deliveries). Returns {reconciled: N}."""
    logger.info("Starting reconcile_work_order_mirror")
    db = db_session_adapter.create_session()
    try:
        from app.services import work_orders_mirror

        count = work_orders_mirror.reconcile_all(db)
        logger.info("Completed reconcile_work_order_mirror: reconciled=%s", count)
        return {"reconciled": count}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
