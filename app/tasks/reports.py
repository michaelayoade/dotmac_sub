"""Scheduled reporting tasks."""

import logging
import time

from app.celery_app import celery_app
from app.db import SessionLocal
from app.services import ncc_report_email
from app.services.observability import record_task_run
from app.tasks._postgres_lock import postgres_session_advisory_lock

logger = logging.getLogger(__name__)

_NCC_EMAIL_TASK = "app.tasks.reports.send_scheduled_ncc_report"
_NCC_EMAIL_LOCK_KEY = 3_281_600_341


@celery_app.task(name=_NCC_EMAIL_TASK)
def send_scheduled_ncc_report() -> dict[str, object]:
    """Weekly NCC complaints digest. Idempotent per local send-date; the
    service short-circuits when disabled or already sent today."""
    started = time.monotonic()
    with postgres_session_advisory_lock(_NCC_EMAIL_LOCK_KEY) as acquired:
        if not acquired:
            result: dict[str, object] = {
                "sent": False,
                "reason": "already_running",
            }
        else:
            session = SessionLocal()
            try:
                result = ncc_report_email.run_scheduled_ncc_report_email(session)
            except Exception:
                session.rollback()
                logger.exception("ncc_report_email_task_failed")
                record_task_run(
                    _NCC_EMAIL_TASK,
                    status="error",
                    counters={},
                    duration_seconds=time.monotonic() - started,
                )
                raise
            finally:
                session.close()

    record_task_run(
        _NCC_EMAIL_TASK,
        status="success",
        counters={"sent": 1 if result.get("sent") else 0},
        duration_seconds=time.monotonic() - started,
    )
    return result
