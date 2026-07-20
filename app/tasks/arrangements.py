"""Scheduled payment-arrangement maintenance tasks."""

from __future__ import annotations

import logging

from app.celery_app import celery_app
from app.services.billing_settings import billing_enabled
from app.services.db_session_adapter import db_session_adapter
from app.services.payment_arrangements import payment_arrangements

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.arrangements.check_overdue_arrangements")
def check_overdue_arrangements() -> dict[str, int]:
    """Advance due installments and default arrangements with repeated misses."""
    logger.info("Starting payment arrangement overdue check")
    with db_session_adapter.session() as session:
        if not billing_enabled(session):
            logger.info(
                "arrangement check skipped: local billing disabled (billing_enabled)"
            )
            return {
                "installments_marked_overdue": 0,
                "installments_marked_due": 0,
                "arrangements_defaulted": 0,
            }
        result = payment_arrangements.check_overdue_installments(session)
        marked_overdue = (
            result.get("installments_marked_overdue", 0)
            if isinstance(result, dict)
            else 0
        )
        marked_due = (
            result.get("installments_marked_due", 0) if isinstance(result, dict) else 0
        )
        defaulted = (
            result.get("arrangements_defaulted", 0) if isinstance(result, dict) else 0
        )
        logger.info(
            "Arrangement overdue check completed: overdue=%d due=%d defaulted=%d",
            marked_overdue,
            marked_due,
            defaulted,
        )
        session.commit()
        return {
            "installments_marked_overdue": marked_overdue,
            "installments_marked_due": marked_due,
            "arrangements_defaulted": defaulted,
        }
