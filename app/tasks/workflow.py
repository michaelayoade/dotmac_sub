"""Workflow automation tasks.

SLA monitoring has been removed as part of CRM cleanup.
This module is kept as a placeholder for future workflow automation tasks.
"""

import logging

from app.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.workflow.placeholder")
def placeholder() -> dict:
    """Placeholder task - SLA monitoring has been removed."""
    logger.info("Workflow placeholder task - no action needed")
    return {"status": "ok"}
