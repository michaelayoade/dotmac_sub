"""Scheduled payment reconciliation maintenance tasks."""

from __future__ import annotations

import logging

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter
from app.services.payment_reconciliation import reconcile_pending_topups

logger = logging.getLogger(__name__)
SessionLocal = db_session_adapter.create_session


@celery_app.task(name="app.tasks.payment_reconciliation.reconcile_topups")
def reconcile_topups() -> dict[str, int]:
    """Sweep stranded top-up intents against the gateway verify API."""
    logger.info("Starting top-up payment reconciliation sweep")
    session = SessionLocal()
    try:
        result = reconcile_pending_topups(session)
        logger.info(
            "Top-up reconciliation completed: checked=%d recovered=%d "
            "linked=%d expired=%d errors=%d",
            result.get("checked", 0),
            result.get("recovered", 0),
            result.get("linked", 0),
            result.get("expired", 0),
            result.get("errors", 0),
        )
        session.commit()
        return result
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
