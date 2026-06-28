"""Admin operational alert evaluation tasks."""

from __future__ import annotations

import logging

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.admin_alerts.evaluate_infrastructure_alerts")
def evaluate_infrastructure_alerts() -> dict[str, int]:
    """Evaluate infrastructure health and sync admin-facing alerts."""
    logger.info("Starting infrastructure admin alert evaluation")
    with db_session_adapter.session() as db:
        from app.services import admin_alerts

        result = admin_alerts.run_infrastructure_alert_evaluation(db)
    logger.info("Infrastructure admin alert evaluation complete: %s", result)
    return result
