"""Scheduled autopay: charge saved cards for accounts with due invoices.

Register in app/tasks/__init__.py and schedule in the beat config to run it in
production (e.g. daily). The charge itself is idempotent on the provider's
external transaction id via the payment recorder.
"""

import logging

from app.celery_app import celery_app
from app.services import autopay as autopay_service
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)
SessionLocal = db_session_adapter.create_session


@celery_app.task(name="app.tasks.autopay.charge_due_invoices")
def charge_due_invoices() -> dict:
    """Run autopay for every active mandate with open invoices."""
    session = SessionLocal()
    try:
        result = autopay_service.run_all_due(session)
        if "total" in result:
            result["total"] = str(result["total"])
        logger.info("autopay run complete: %s", result)
        return result
    finally:
        session.close()
