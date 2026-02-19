"""Central event dispatcher for the event system.

This module provides the main entry point for emitting events throughout
the application. When an event is emitted, it is routed to all registered
handlers (webhook, lifecycle, notification, audit).

Events are persisted before dispatching to enable retry of failed handlers.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.services.events.types import Event, EventType

logger = logging.getLogger(__name__)


class EventDispatcher:
    """Central dispatcher that routes events to all registered handlers.

    Events are persisted to the event_store table before dispatching,
    enabling retry of failed handlers and providing an audit trail.
    """

    def __init__(self):
        self._handlers: list = []

    def register_handler(self, handler):
        """Register an event handler."""
        self._handlers.append(handler)

    def dispatch(self, db: Session, event: Event) -> None:
        """Dispatch an event to all registered handlers.

        The event is persisted before dispatching to enable retry on failure.
        Each handler is called in sequence. Failed handlers are tracked
        for later retry.

        Args:
            db: Database session for handlers that need DB access
            event: The event to dispatch
        """
        from app.models.event_store import EventStatus, EventStore

        logger.debug(
            f"Dispatching event {event.event_type.value} (id={event.event_id})"
        )

        # 1. Persist event before processing
        event_record: EventStore | None = EventStore(
            event_id=event.event_id,
            event_type=event.event_type.value,
            payload=event.payload,
            status=EventStatus.processing,
            actor=event.actor,
            subscriber_id=event.subscriber_id,
            account_id=event.account_id,
            subscription_id=event.subscription_id,
            invoice_id=event.invoice_id,
            service_order_id=event.service_order_id,
        )
        db.add(event_record)
        try:
            db.commit()
        except Exception as persist_exc:
            # If we can't persist, still try to process but log the error
            logger.warning(
                f"Failed to persist event {event.event_id} to event_store: {persist_exc}"
            )
            db.rollback()
            event_record = None

        # 2. Process all handlers, tracking failures
        failed_handlers: list[dict[str, str]] = []
        for handler in self._handlers:
            try:
                handler.handle(db, event)
            except Exception as exc:
                handler_name = handler.__class__.__name__
                logger.exception(
                    f"Handler {handler_name} failed for event "
                    f"{event.event_type.value}: {exc}"
                )
                failed_handlers.append({
                    "handler": handler_name,
                    "error": str(exc),
                })

        # 3. Update event status
        if event_record:
            try:
                if failed_handlers:
                    event_record.status = EventStatus.failed
                    event_record.failed_handlers = failed_handlers
                    event_record.error = json.dumps([fh["error"] for fh in failed_handlers])
                else:
                    event_record.status = EventStatus.completed
                event_record.processed_at = datetime.now(timezone.utc)
                db.commit()
            except Exception as update_exc:
                logger.warning(
                    f"Failed to update event_store status for {event.event_id}: {update_exc}"
                )
                db.rollback()

    def retry_event(self, db: Session, event_record) -> bool:
        """Retry processing a failed event.

        Args:
            db: Database session
            event_record: The EventStore record to retry

        Returns:
            True if all handlers succeeded, False otherwise
        """
        from app.models.event_store import EventStatus

        # Reconstruct the Event from stored data
        event = Event(
            event_type=EventType(event_record.event_type),
            payload=event_record.payload,
            event_id=event_record.event_id,
            actor=event_record.actor,
            subscriber_id=event_record.subscriber_id,
            account_id=event_record.account_id,
            subscription_id=event_record.subscription_id,
            invoice_id=event_record.invoice_id,
            service_order_id=event_record.service_order_id,
        )

        # Get handlers that failed previously
        failed_handler_names = set()
        if event_record.failed_handlers:
            failed_handler_names = {fh["handler"] for fh in event_record.failed_handlers}

        # Update retry count and status
        event_record.retry_count += 1
        event_record.status = EventStatus.processing
        db.commit()

        # Retry only failed handlers (or all if no specific failures recorded)
        new_failures: list[dict] = []
        for handler in self._handlers:
            handler_name = handler.__class__.__name__
            # Only retry failed handlers, or all if we don't know which failed
            if failed_handler_names and handler_name not in failed_handler_names:
                continue
            try:
                handler.handle(db, event)
            except Exception as exc:
                logger.exception(
                    f"Handler {handler_name} failed on retry for event "
                    f"{event.event_type.value}: {exc}"
                )
                new_failures.append({
                    "handler": handler_name,
                    "error": str(exc),
                })

        # Update final status
        if new_failures:
            event_record.status = EventStatus.failed
            event_record.failed_handlers = new_failures
            event_record.error = json.dumps([fh["error"] for fh in new_failures])
        else:
            event_record.status = EventStatus.completed
            event_record.failed_handlers = None
            event_record.error = None
        event_record.processed_at = datetime.now(timezone.utc)
        db.commit()

        return len(new_failures) == 0


# Global dispatcher instance
_dispatcher: EventDispatcher | None = None


def get_dispatcher() -> EventDispatcher:
    """Get the global event dispatcher, initializing handlers if needed."""
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = EventDispatcher()
        _initialize_handlers(_dispatcher)
    return _dispatcher


def _initialize_handlers(dispatcher: EventDispatcher) -> None:
    """Initialize and register all event handlers."""
    from app.services.events.handlers.webhook import WebhookHandler
    from app.services.events.handlers.lifecycle import LifecycleHandler
    from app.services.events.handlers.notification import NotificationHandler
    from app.services.events.handlers.provisioning import ProvisioningHandler
    from app.services.events.handlers.enforcement import EnforcementHandler

    dispatcher.register_handler(WebhookHandler())
    dispatcher.register_handler(LifecycleHandler())
    dispatcher.register_handler(NotificationHandler())
    dispatcher.register_handler(ProvisioningHandler())
    dispatcher.register_handler(EnforcementHandler())

    logger.info(
        "Event handlers initialized: webhook, lifecycle, notification, provisioning, enforcement"
    )


def emit_event(
    db: Session,
    event_type: EventType,
    payload: dict[str, Any],
    *,
    actor: str | None = None,
    subscriber_id: UUID | str | None = None,
    account_id: UUID | str | None = None,
    subscription_id: UUID | str | None = None,
    invoice_id: UUID | str | None = None,
    service_order_id: UUID | str | None = None,
) -> Event:
    """Emit an event to all registered handlers.

    This is the main entry point for services to emit events. After calling
    this function, the event will be:
    - Delivered to subscribed webhook endpoints (via Celery task)
    - Recorded as a lifecycle event (if applicable)
    - Queued as a notification (if template configured)

    Args:
        db: Database session
        event_type: The type of event
        payload: Event-specific data
        actor: Who/what triggered the event
        subscriber_id: Related subscriber ID
        account_id: Related account ID
        subscription_id: Related subscription ID
        invoice_id: Related invoice ID
        service_order_id: Related service order ID

    Returns:
        The created Event object

    Example:
        from app.services.events import emit_event
        from app.services.events.types import EventType

        emit_event(
            db,
            EventType.subscription_activated,
            {"subscription_id": str(sub.id), "offer_name": sub.offer.name},
            subscription_id=sub.id,
            account_id=sub.account_id,
        )
    """
    # Normalize UUIDs
    def to_uuid(value: UUID | str | None) -> UUID | None:
        if value is None:
            return None
        if isinstance(value, UUID):
            return value
        return UUID(value)

    event = Event(
        event_type=event_type,
        payload=payload,
        actor=actor,
        subscriber_id=to_uuid(subscriber_id),
        account_id=to_uuid(account_id),
        subscription_id=to_uuid(subscription_id),
        invoice_id=to_uuid(invoice_id),
        service_order_id=to_uuid(service_order_id),
    )

    dispatcher = get_dispatcher()
    dispatcher.dispatch(db, event)

    logger.info(
        f"Event emitted: {event_type.value} (id={event.event_id})"
    )

    return event
