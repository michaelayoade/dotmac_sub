"""Central event dispatcher for the event system.

This module provides the main entry point for emitting events throughout
the application. When an event is emitted, it is routed to all registered
handlers (webhook, lifecycle, notification, audit).

Events are persisted before dispatching to enable retry of failed handlers.
"""

import json
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.services.events.types import Event, EventType

logger = logging.getLogger(__name__)


def _event_extra(
    event: Event,
    *,
    handler_count: int | None = None,
    failed_handlers: list[dict[str, str]] | None = None,
    retry_count: int | None = None,
) -> dict[str, object]:
    extra: dict[str, object] = {
        "event": "domain_event",
        "event_id": str(event.event_id),
        "event_type": event.event_type.value,
        "actor": event.actor,
        "subscriber_id": str(event.subscriber_id) if event.subscriber_id else None,
        "account_id": str(event.account_id) if event.account_id else None,
        "subscription_id": str(event.subscription_id)
        if event.subscription_id
        else None,
        "invoice_id": str(event.invoice_id) if event.invoice_id else None,
        "service_order_id": str(event.service_order_id)
        if event.service_order_id
        else None,
    }
    if handler_count is not None:
        extra["handler_count"] = handler_count
    if failed_handlers is not None:
        extra["failed_handlers"] = failed_handlers
        extra["failed_handler_count"] = len(failed_handlers)
    if retry_count is not None:
        extra["retry_count"] = retry_count
    return extra


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

        logger.info(
            "event_dispatch_start",
            extra=_event_extra(event, handler_count=len(self._handlers)),
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
                "event_persist_failed",
                extra={
                    **_event_extra(event, handler_count=len(self._handlers)),
                    "error": str(persist_exc),
                },
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
                    "event_handler_failed",
                    extra={
                        **_event_extra(event, handler_count=len(self._handlers)),
                        "handler": handler_name,
                        "error": str(exc),
                    },
                )
                failed_handlers.append(
                    {
                        "handler": handler_name,
                        "error": str(exc),
                    }
                )

        # 3. Update event status
        if event_record:
            try:
                if failed_handlers:
                    event_record.status = EventStatus.failed
                    event_record.failed_handlers = failed_handlers
                    event_record.error = json.dumps(
                        [fh["error"] for fh in failed_handlers]
                    )
                else:
                    event_record.status = EventStatus.completed
                event_record.processed_at = datetime.now(UTC)
                db.commit()
            except Exception as update_exc:
                logger.warning(
                    "event_status_update_failed",
                    extra={
                        **_event_extra(
                            event,
                            handler_count=len(self._handlers),
                            failed_handlers=failed_handlers,
                        ),
                        "error": str(update_exc),
                    },
                )
                db.rollback()
        logger.info(
            "event_dispatch_complete",
            extra=_event_extra(
                event,
                handler_count=len(self._handlers),
                failed_handlers=failed_handlers,
            ),
        )

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
            failed_handler_names = {
                fh["handler"] for fh in event_record.failed_handlers
            }

        # Update retry count and status
        event_record.retry_count += 1
        event_record.status = EventStatus.processing
        db.commit()
        logger.info(
            "event_retry_start",
            extra=_event_extra(
                event,
                handler_count=len(self._handlers),
                retry_count=event_record.retry_count,
            ),
        )

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
                    "event_retry_handler_failed",
                    extra={
                        **_event_extra(
                            event,
                            handler_count=len(self._handlers),
                            retry_count=event_record.retry_count,
                        ),
                        "handler": handler_name,
                        "error": str(exc),
                    },
                )
                new_failures.append(
                    {
                        "handler": handler_name,
                        "error": str(exc),
                    }
                )

        # Update final status
        if new_failures:
            event_record.status = EventStatus.failed
            event_record.failed_handlers = new_failures
            event_record.error = json.dumps([fh["error"] for fh in new_failures])
        else:
            event_record.status = EventStatus.completed
            event_record.failed_handlers = None
            event_record.error = None
        event_record.processed_at = datetime.now(UTC)
        db.commit()
        logger.info(
            "event_retry_complete",
            extra=_event_extra(
                event,
                handler_count=len(self._handlers),
                failed_handlers=new_failures,
                retry_count=event_record.retry_count,
            ),
        )

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
    from app.services.events.handlers.crm_sync import CrmSyncHandler
    from app.services.events.handlers.enforcement import EnforcementHandler
    from app.services.events.handlers.integration_hook import IntegrationHookHandler
    from app.services.events.handlers.lifecycle import LifecycleHandler
    from app.services.events.handlers.notification import NotificationHandler
    from app.services.events.handlers.provisioning import ProvisioningHandler
    from app.services.events.handlers.webhook import WebhookHandler

    dispatcher.register_handler(WebhookHandler())
    dispatcher.register_handler(IntegrationHookHandler())
    dispatcher.register_handler(LifecycleHandler())
    dispatcher.register_handler(NotificationHandler())
    dispatcher.register_handler(ProvisioningHandler())
    dispatcher.register_handler(EnforcementHandler())
    dispatcher.register_handler(CrmSyncHandler())

    logger.info(
        "Event handlers initialized: webhook, integration_hooks, lifecycle, notification, provisioning, enforcement, crm_sync",
        extra={
            "event": "event_handlers_initialized",
            "handler_count": len(dispatcher._handlers),
        },
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
        "event_emitted",
        extra=_event_extra(event, handler_count=len(dispatcher._handlers)),
    )

    return event
