import logging

from app.celery_app import celery_app
from app.db import SessionLocal
from app.services import billing_automation as billing_automation_service

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.billing.run_invoice_cycle")
def run_invoice_cycle() -> dict[str, int]:
    logger.info("Starting billing invoice cycle")
    session = SessionLocal()
    try:
        result = billing_automation_service.run_invoice_cycle(session)
        processed = result.get("subscriptions_billed", 0)
        errors = result.get("errors", 0)
        logger.info(
            "Billing invoice cycle completed: %d billed, %d invoices created, %d errors",
            processed,
            result.get("invoices_created", 0),
            errors,
        )
        return {"processed": processed, "errors": errors}
    except Exception as e:
        logger.error("Billing invoice cycle failed: %s", e)
        session.rollback()
        raise
    finally:
        session.close()
