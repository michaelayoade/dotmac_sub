"""Retired workflow task compatibility shims."""

from __future__ import annotations

import logging

from app.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.workflow.detect_sla_breaches")
def detect_sla_breaches() -> dict[str, object]:
    """Consume queued messages for a retired scheduler task without error logs."""
    logger.info("Discarded retired detect_sla_breaches task.")
    return {"retired": True}
