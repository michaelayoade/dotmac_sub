"""Scheduled payment-arrangement maintenance tasks."""

from __future__ import annotations

import logging

from app.celery_app import celery_app
from app.services import payment_arrangements
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)
SessionLocal = db_session_adapter.create_session


@celery_app.task(name="app.tasks.arrangements.check_overdue_arrangements")
def check_overdue_arrangements() -> dict[str, int]:
    """Mark due arrangement installments overdue and default repeated misses."""
    session = SessionLocal()
    try:
        overdue_count = (
            payment_arrangements.payment_arrangements.check_overdue_installments(
                session
            )
        )
        result = {"overdue_installments": overdue_count}
        logger.info("payment arrangement overdue check complete: %s", result)
        return result
    finally:
        session.close()
