"""Celery tasks for catalog/subscription operations."""

import logging

from app.celery_app import celery_app
from app.db import SessionLocal
from app.services.catalog import subscriptions as subscriptions_service

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.catalog.expire_subscriptions")
def expire_subscriptions() -> dict:
    """Expire subscriptions that have passed their end_at date."""
    logger.info("Starting expire_subscriptions")
    session = SessionLocal()
    try:
        result = subscriptions_service.Subscriptions.expire_subscriptions(session)
        logger.info("Completed expire_subscriptions: %s", result)
        return result
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
