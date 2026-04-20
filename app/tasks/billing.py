import logging
from datetime import UTC, datetime

from app.celery_app import celery_app
from app.services import billing_automation as billing_automation_service
from app.services.db_session_adapter import db_session_adapter
from app.services.task_idempotency import idempotent_task

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.billing.run_invoice_cycle")
@idempotent_task(
    key_func=lambda: f"billing_cycle:{datetime.now(UTC).strftime('%Y-%m-%d')}"
)
def run_invoice_cycle() -> dict[str, int]:
    logger.info("Starting billing invoice cycle")
    with db_session_adapter.session() as session:
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


@celery_app.task(name="app.tasks.billing.mark_invoices_overdue")
@idempotent_task(
    key_func=lambda: f"overdue_check:{datetime.now(UTC).strftime('%Y-%m-%d-%H')}"
)
def mark_invoices_overdue() -> dict[str, int]:
    """Hourly task: detect past-due invoices and trigger enforcement."""
    logger.info("Starting overdue invoice detection")
    with db_session_adapter.session() as session:
        result = billing_automation_service.mark_overdue_invoices(session)
        logger.info(
            "Overdue detection completed: %d marked",
            result.get("marked_overdue", 0),
        )
        return result
