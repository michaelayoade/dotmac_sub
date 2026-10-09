"""Periodic delivery adapter for scheduled Automation Center rules."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from app.celery_app import celery_app
from app.services import automation_scheduled
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.automation.run_scheduled_automation_rules")
def run_scheduled_automation_rules(*, now_iso: str | None = None) -> dict[str, int]:
    now = datetime.fromisoformat(now_iso) if now_iso else datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    with db_session_adapter.owner_command_session() as session:
        outcome = automation_scheduled.enqueue_scheduled_events(
            session,
            command=automation_scheduled.EnqueueScheduledAutomationCommand(
                now=now,
                context=CommandContext.system(
                    actor="automation-scheduler",
                    scope="automation:runtime",
                    reason="Evaluate due scheduled rules and stage their target events",
                ),
            ),
        )
    result = {
        "rules_claimed": outcome.rules_claimed,
        "events_emitted": outcome.events_emitted,
        "rules_skipped": outcome.rules_skipped,
    }
    logger.info("scheduled automation evaluation complete", extra=result)
    return result
