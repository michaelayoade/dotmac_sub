"""Celery task: deliver the ``field_erp_sync_events`` outbox to DotMac ERP.

Beat-driven sweep. Gated by ``dotmac_erp_sync_enabled`` (default off) at the
scheduler, so it is inert until a flow is cut over to sub. Delivery itself is
further gated per-flow by ``sync_flow_ownership`` inside ``deliver_pending`` — a
row for a flow sub does not own is skipped, never posted.

Reliability contract: BEAT_RERUN. Each row carries a stable idempotency key
(sent to ERP), transient failures leave the row pending for the next run, and
permanent / budget-exhausted rows dead-letter in the table itself — so a failed
run self-heals and re-delivery is safe.
"""

from __future__ import annotations

import logging
import time

from app.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.dotmac_erp_outbox.deliver_erp_sync_events")
def deliver_erp_sync_events() -> dict:
    """Deliver pending field_erp_sync_events rows to ERP."""
    from app.metrics import observe_job

    start = time.monotonic()
    status = "success"
    logger.info("DELIVER_ERP_SYNC_EVENTS_START")
    results: dict[str, object] = {}
    try:
        from app.db import task_session
        from app.services.dotmac_erp import outbox

        with task_session() as db:
            results = outbox.deliver_pending(db).as_dict()
    except Exception:
        status = "error"
        raise
    finally:
        observe_job("deliver_erp_sync_events", status, time.monotonic() - start)

    logger.info("DELIVER_ERP_SYNC_EVENTS_COMPLETE %s", results)
    return results


@celery_app.task(name="app.tasks.dotmac_erp_outbox.refresh_expense_claim_statuses")
def refresh_expense_claim_statuses() -> dict:
    """Poll ERP for in-flight expense-claim statuses and refresh mirror fields.

    Read-only reconcile: for each synced FieldExpenseRequest still awaiting an ERP
    decision, GET the claim status and write it back. Gated at the scheduler by
    ``dotmac_erp_sync_enabled`` (default off), so it is inert until cutover; a
    no-op when nothing is in flight. Idempotent — safe to re-run.
    """
    from app.metrics import observe_job

    start = time.monotonic()
    status = "success"
    logger.info("REFRESH_EXPENSE_CLAIM_STATUSES_START")
    results: dict[str, object] = {}
    try:
        from app.db import task_session
        from app.services.dotmac_erp.expense_sync import refresh_expense_claim_statuses

        with task_session() as db:
            results = refresh_expense_claim_statuses(db)
    except Exception:
        status = "error"
        raise
    finally:
        observe_job("refresh_expense_claim_statuses", status, time.monotonic() - start)

    logger.info("REFRESH_EXPENSE_CLAIM_STATUSES_COMPLETE %s", results)
    return results
