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


@celery_app.task(name="app.tasks.work_orders.refresh_work_order_mirror_for_subscriber")
def refresh_work_order_mirror_for_subscriber(subscriber_id: str) -> dict[str, bool]:
    """Refresh one subscriber's work-order mirror from the CRM — enqueued by a
    stale on-view read so the request doesn't block on the CRM round-trip."""
    from app.services import work_orders_mirror
    from app.services.crm_client import CRMClientError

    db = db_session_adapter.create_session()
    try:
        ok = work_orders_mirror.reconcile_subscriber(db, subscriber_id)
        return {"refreshed": bool(ok)}
    except CRMClientError as exc:
        db.rollback()
        logger.warning(
            "refresh_work_order_mirror_failed subscriber=%s: %s", subscriber_id, exc
        )
        return {"refreshed": False}
    finally:
        db.close()
