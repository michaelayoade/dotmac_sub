"""Event system module.

Provides a centralized event dispatcher for firing webhooks, recording
lifecycle events, and queuing customer notifications.

Usage:
    from app.services.events import emit_event
    from app.services.events.types import EventType

    # In a service after a state change:
    emit_event(
        db,
        EventType.subscription_activated,
        {"subscription_id": str(sub.id), "offer_name": sub.offer.name},
        subscription_id=sub.id,
        account_id=sub.account_id,
    )
"""

from app.services.events.dispatcher import emit_event
from app.services.events.types import Event, EventType

__all__ = ["emit_event", "Event", "EventType"]
