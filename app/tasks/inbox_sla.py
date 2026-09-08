"""Idempotent Inbox SLA evaluator."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import or_

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.inbox_sla.evaluate_inbox_sla")
def evaluate_inbox_sla(*, limit: int = 500) -> dict[str, int]:
    from app.models.inbox_sla import InboxSlaClock
    from app.services.inbox_sla import evaluate_clock

    now = datetime.now(UTC)
    with db_session_adapter.owner_command_session() as db:
        clocks = (
            db.query(InboxSlaClock)
            .filter(InboxSlaClock.status.in_(["running", "warning", "paused"]))
            .filter(
                or_(
                    InboxSlaClock.first_response_due_at <= now,
                    InboxSlaClock.next_response_due_at <= now,
                    InboxSlaClock.resolution_due_at <= now,
                )
            )
            .order_by(InboxSlaClock.first_response_due_at.asc())
            .with_for_update(skip_locked=True)
            .limit(max(1, min(limit, 5000)))
            .all()
        )
        counts = {
            "checked": 0,
            "warning": 0,
            "breached": 0,
            "completed": 0,
            "paused": 0,
        }
        for clock in clocks:
            status = evaluate_clock(db, clock.id, now=now)
            counts["checked"] += 1
            if status in counts:
                counts[status] += 1
        logger.info("inbox_sla_evaluation_complete", extra=counts)
        return counts
