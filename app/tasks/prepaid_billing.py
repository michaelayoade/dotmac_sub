"""Celery task: periodic prepaid drawdown charges.

Posts one prepaid charge per due subscription (see
``app.services.prepaid_billing``). Gated by ``billing_enabled`` so it stays
inert in shadow mode and activates with the rest of local billing at cutover.
"""

import logging

from app.celery_app import celery_app
from app.services.billing_settings import billing_enabled
from app.services.db_session_adapter import db_session_adapter
from app.services.prepaid_billing import run_prepaid_charges

logger = logging.getLogger(__name__)
SessionLocal = db_session_adapter.create_session


@celery_app.task(name="app.tasks.prepaid_billing.run_prepaid_charges")
def run_prepaid_charges_task() -> dict:
    logger.info("Starting prepaid drawdown charge run")
    session = SessionLocal()
    try:
        if not billing_enabled(session):
            logger.info(
                "prepaid charges skipped: local billing disabled (billing_enabled)"
            )
            return {"skipped": "billing_disabled"}
        summary = run_prepaid_charges(session, dry_run=False)
        logger.info(
            "Prepaid charges completed: scanned=%s initialised=%s charged=%s "
            "skipped_zero_price=%s total_charged=%s",
            summary["scanned"],
            summary["initialised"],
            summary["charged"],
            summary["skipped_zero_price"],
            summary["total_charged"],
        )
        return summary
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
