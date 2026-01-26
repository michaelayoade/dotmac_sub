"""Celery tasks for catalog/subscription operations."""

from app.celery_app import celery_app
from app.db import SessionLocal
from app.services.catalog import subscriptions as subscriptions_service


@celery_app.task(name="app.tasks.catalog.expire_subscriptions")
def expire_subscriptions():
    """Expire subscriptions that have passed their end_at date."""
    session = SessionLocal()
    try:
        result = subscriptions_service.Subscriptions.expire_subscriptions(session)
        return result
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
