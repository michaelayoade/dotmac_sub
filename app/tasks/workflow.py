"""Workflow automation tasks for SLA monitoring and ticket management."""

import logging
from datetime import datetime, timezone

from app.celery_app import celery_app
from app.db import SessionLocal
from app.models.workflow import SlaClock, SlaClockStatus
from app.schemas.workflow import SlaBreachCreate
from app.services import workflow as workflow_service

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.workflow.detect_sla_breaches")
def detect_sla_breaches() -> dict[str, int]:
    """
    Detect SLA breaches by checking all running SLA clocks.

    Runs every 30 minutes to identify SLA clocks that have exceeded their due_at
    time and creates SlaBreach records for them.

    Returns:
        dict with counts of checked clocks and breaches created
    """
    session = SessionLocal()
    checked = 0
    breached = 0
    errors = 0
    try:
        now = datetime.now(timezone.utc)

        # Find all running SLA clocks that are past due
        overdue_clocks = (
            session.query(SlaClock)
            .filter(SlaClock.status == SlaClockStatus.running)
            .filter(SlaClock.due_at < now)
            .filter(SlaClock.breached_at.is_(None))
            .all()
        )

        checked = len(overdue_clocks)
        logger.info("SLA breach detection: found %d overdue clocks", checked)

        for clock in overdue_clocks:
            try:
                payload = SlaBreachCreate(
                    clock_id=clock.id,
                    breached_at=now,
                    notes=f"Auto-detected SLA breach at {now.isoformat()}",
                )
                workflow_service.sla_breaches.create(session, payload)
                breached += 1
                logger.debug(
                    "Created SLA breach for clock %s (entity: %s/%s)",
                    clock.id,
                    clock.entity_type.value,
                    clock.entity_id,
                )
            except Exception as exc:
                errors += 1
                logger.exception(
                    "Failed to create SLA breach for clock %s: %s",
                    clock.id,
                    exc,
                )
                session.rollback()
                continue

    except Exception:
        session.rollback()
        logger.exception("SLA breach detection task failed")
        raise
    finally:
        session.close()

    logger.info(
        "SLA breach detection complete: checked=%d, breached=%d, errors=%d",
        checked,
        breached,
        errors,
    )
    return {"checked": checked, "breached": breached, "errors": errors}
