"""Celery task: periodic prepaid drawdown charges.

Posts one prepaid charge per due subscription (see
``app.services.prepaid_billing``). Gated by ``billing_enabled`` so it stays
inert in shadow mode and activates with the rest of local billing at cutover.
"""

import logging
from datetime import UTC, datetime

from app.celery_app import celery_app
from app.services.billing_settings import billing_enabled, check_billing_switch
from app.services.db_session_adapter import db_session_adapter
from app.services.prepaid_billing import run_prepaid_charges
from app.services.task_idempotency import idempotent_task

logger = logging.getLogger(__name__)
SessionLocal = db_session_adapter.create_session


@celery_app.task(name="app.tasks.prepaid_billing.check_billing_switch")
def check_billing_switch_task() -> dict:
    """Config-integrity guard: alert if billing_enabled drifts from expected.

    NOT gated by billing_enabled — it must run precisely to catch an unexpected
    flip (the mechanism behind the earlier phantom-invoice incident). Logs at
    CRITICAL and emits an event when the live switch != the pinned expected
    value so the drift is visible instead of silently re-arming billing.
    """
    session = SessionLocal()
    try:
        result = check_billing_switch(session)
        if not result["ok"]:
            # CRITICAL log is the alert signal (wire a log-based alert to it):
            # billing_enabled drifted from the pinned expected value.
            logger.critical(
                "billing_switch_drift: billing_enabled=%s expected=%s — "
                "local billing may act on customers unexpectedly",
                result["actual"],
                result["expected"],
            )
        return result
    finally:
        session.close()


@celery_app.task(name="app.tasks.prepaid_billing.run_prepaid_charges")
@idempotent_task(
    key_func=lambda: f"prepaid_charges:{datetime.now(UTC).strftime('%Y-%m-%d')}"
)
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
