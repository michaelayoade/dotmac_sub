"""Device projection reconcile Celery task.

Keeps the materialised ``device_projections`` table fresh by running the
``network.device_projection`` reconciler on a schedule. The reconciler is the
sole canonical writer; this task is a thin transport that hands it a session.
Wire ``reconcile_device_projections`` into Celery beat. See
app/services/device_projection_reconcile.py.
"""

import logging

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.device_projection.reconcile_device_projections")
def reconcile_device_projections() -> dict:
    """Rebuild the unified device projection from the authoritative tables."""
    logger.info("Starting device projection reconcile task")
    with db_session_adapter.session() as db:
        from app.services import device_projection_reconcile as svc

        result = svc.reconcile_device_projections(db)
        logger.info(
            "Device projection reconcile complete: inserted=%d updated=%d pruned=%d",
            result.inserted,
            result.updated,
            result.pruned,
        )
        return {
            "inserted": result.inserted,
            "updated": result.updated,
            "pruned": result.pruned,
            "total": result.total,
        }
