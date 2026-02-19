"""Celery tasks for event system maintenance.

Handles retry of failed events and cleanup of old event records.
"""

import logging
from datetime import UTC, datetime, timedelta

from app.celery_app import celery_app
from app.db import SessionLocal

logger = logging.getLogger(__name__)

# Configuration
MAX_RETRIES = 3
MAX_EVENT_AGE_HOURS = 24
BATCH_SIZE = 100


@celery_app.task(name="app.tasks.events.retry_failed_events")
def retry_failed_events():
    """Retry events that failed processing.

    This task finds events that failed handler processing and retries them.
    Events are retried up to MAX_RETRIES times within MAX_EVENT_AGE_HOURS.
    """
    from app.models.event_store import EventStatus, EventStore
    from app.services.events.dispatcher import get_dispatcher

    session = SessionLocal()
    try:
        cutoff = datetime.now(UTC) - timedelta(hours=MAX_EVENT_AGE_HOURS)

        # Find failed events eligible for retry
        failed_events = (
            session.query(EventStore)
            .filter(EventStore.status == EventStatus.failed)
            .filter(EventStore.retry_count < MAX_RETRIES)
            .filter(EventStore.created_at > cutoff)
            .filter(EventStore.is_active.is_(True))
            .order_by(EventStore.created_at.asc())
            .limit(BATCH_SIZE)
            .all()
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
                logger.exception(
                    f"Error retrying event {event_record.event_id}: {exc}"
                )
                session.rollback()

        result = {
            "retried": len(failed_events),
            "succeeded": succeeded,
            "failed": failed,
        }
        logger.info(f"Event retry task completed: {result}")
        return result

    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@celery_app.task(name="app.tasks.events.cleanup_old_events")
def cleanup_old_events(retention_days: int = 30):
    """Clean up old completed events from the event store.

    Args:
        retention_days: Number of days to retain completed events

    This task removes completed events older than retention_days.
    Failed events are kept longer for debugging purposes.
    """
    from app.models.event_store import EventStatus, EventStore

    session = SessionLocal()
    try:
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)

        # Delete old completed events
        deleted_count = (
            session.query(EventStore)
            .filter(EventStore.status == EventStatus.completed)
            .filter(EventStore.created_at < cutoff)
            .delete(synchronize_session=False)
        )

        session.commit()

        logger.info(f"Cleaned up {deleted_count} old completed events")
        return {"deleted": deleted_count}

    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@celery_app.task(name="app.tasks.events.mark_stale_processing_events")
def mark_stale_processing_events(stale_minutes: int = 30):
    """Mark stuck processing events as failed.

    Events that have been in 'processing' status for longer than
    stale_minutes are marked as failed so they can be retried.

    Args:
        stale_minutes: Minutes after which processing events are considered stuck
    """
    from app.models.event_store import EventStatus, EventStore

    session = SessionLocal()
    try:
        cutoff = datetime.now(UTC) - timedelta(minutes=stale_minutes)

        # Find and mark stuck processing events
        stuck_events = (
            session.query(EventStore)
            .filter(EventStore.status == EventStatus.processing)
            .filter(EventStore.updated_at < cutoff)
            .filter(EventStore.is_active.is_(True))
            .all()
        )

        for event_record in stuck_events:
            event_record.status = EventStatus.failed
            event_record.error = "Event processing timed out (marked as stale)"
            logger.warning(
                f"Marked stale processing event as failed: {event_record.event_id}"
            )

        session.commit()

        logger.info(f"Marked {len(stuck_events)} stale processing events as failed")
        return {"marked_failed": len(stuck_events)}

    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
