"""Celery tasks for event system maintenance.

Handles retry of failed events and cleanup of old event records.
"""

import logging

from app.celery_app import celery_app
from app.services import event_store as event_store_service
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)

# Configuration
MAX_RETRIES = 3
MAX_EVENT_AGE_HOURS = 24
BATCH_SIZE = 100

# Advisory lock keys for preventing concurrent task runs
_EVENT_RETRY_LOCK_KEY = 70420801
_EVENT_STALE_LOCK_KEY = 70420802


@celery_app.task(name="app.tasks.events.retry_failed_events")
def retry_failed_events():
    """Retry events that failed processing.

    This task finds events that failed handler processing and retries them.
    Events are retried up to MAX_RETRIES times within MAX_EVENT_AGE_HOURS.
    Uses advisory lock to prevent concurrent runs.
    """
    from app.services.events.dispatcher import get_dispatcher

    with db_session_adapter.advisory_lock(_EVENT_RETRY_LOCK_KEY) as (
        session,
        lock_acquired,
    ):
        if not lock_acquired:
            logger.debug("Skipping event retry: previous run still in progress")
            return {"skipped_due_to_lock": 1}
        failed_events = event_store_service.list_retryable_failed_events(
            session,
            max_retries=MAX_RETRIES,
            max_age_hours=MAX_EVENT_AGE_HOURS,
            limit=BATCH_SIZE,
        )

        if not failed_events:
            return {"retried": 0, "succeeded": 0, "failed": 0}

        dispatcher = get_dispatcher()
        succeeded = 0
        failed = 0

        for event_record in failed_events:
            try:
                success = dispatcher.retry_event(session, event_record)
                if success:
                    succeeded += 1
                    logger.info(
                        f"Successfully retried event {event_record.event_id} "
                        f"({event_record.event_type})"
                    )
                else:
                    failed += 1
                    logger.warning(
                        f"Event {event_record.event_id} failed retry "
                        f"(attempt {event_record.retry_count}/{MAX_RETRIES})"
                    )
            except Exception as exc:
                failed += 1
                logger.exception(f"Error retrying event {event_record.event_id}: {exc}")
                session.rollback()

        result = {
            "retried": len(failed_events),
            "succeeded": succeeded,
            "failed": failed,
        }
        logger.info("Event retry task completed: %s", result)
        return result


@celery_app.task(name="app.tasks.events.cleanup_old_events")
def cleanup_old_events(retention_days: int = 30):
    """Clean up old completed events from the event store.

    Args:
        retention_days: Number of days to retain completed events

    This task removes completed events older than retention_days.
    Failed events are kept longer for debugging purposes.
    """
    with db_session_adapter.session() as session:
        result = event_store_service.cleanup_completed_events(
            session,
            retention_days=retention_days,
        )

        logger.info(
            "Cleaned up %s old completed events and %s handler attempts",
            result["deleted"],
            result["handler_attempts_deleted"],
        )
        return result


@celery_app.task(name="app.tasks.events.mark_stale_processing_events")
def mark_stale_processing_events(stale_minutes: int = 30):
    """Mark stuck processing events as failed.

    Events that have been in 'processing' status for longer than
    stale_minutes are marked as failed so they can be retried.
    Uses advisory lock to prevent concurrent runs.

    Args:
        stale_minutes: Minutes after which processing events are considered stuck
    """
    with db_session_adapter.advisory_lock(_EVENT_STALE_LOCK_KEY) as (
        session,
        lock_acquired,
    ):
        if not lock_acquired:
            logger.debug("Skipping stale event marking: previous run still in progress")
            return {"skipped_due_to_lock": 1}
        marked_failed = event_store_service.mark_stale_processing_events(
            session,
            stale_minutes=stale_minutes,
        )

        logger.info("Marked %s stale processing events as failed", marked_failed)
        return {"marked_failed": marked_failed}
