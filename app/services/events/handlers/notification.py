"""Notification handler for the event system.

Queues customer notifications based on configured notification templates.
"""

import logging

from sqlalchemy.orm import Session

from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationStatus,
    NotificationTemplate,
)
from app.services.events.types import Event, EventType

logger = logging.getLogger(__name__)


# Mapping from EventType to notification template codes
# These codes are used to look up templates in the notification_templates table
EVENT_TYPE_TO_TEMPLATE = {
    # Subscription events
    EventType.subscription_created: "subscription_created",
    EventType.subscription_activated: "subscription_activated",
    EventType.subscription_suspended: "subscription_suspended",
    EventType.subscription_canceled: "subscription_canceled",
    EventType.subscription_expiring: "subscription_expiring",
    # Billing events
    EventType.invoice_created: "invoice_created",
    EventType.invoice_sent: "invoice_sent",
    EventType.invoice_overdue: "invoice_overdue",
    EventType.payment_received: "payment_received",
    EventType.payment_failed: "payment_failed",
    # Usage events
    EventType.usage_warning: "usage_warning",
    EventType.usage_exhausted: "usage_exhausted",
    # Provisioning events
    EventType.provisioning_completed: "provisioning_completed",
    EventType.provisioning_failed: "provisioning_failed",
}


class NotificationHandler:
    """Handler that queues customer notifications."""

    def handle(self, db: Session, event: Event) -> None:
        """Process an event by creating notifications.

        Looks up the notification template for the event type. If found
        and active, creates a Notification record for each configured channel.

        Args:
            db: Database session
            event: The event to process
        """
        # Get template code for this event type
        template_code = EVENT_TYPE_TO_TEMPLATE.get(event.event_type)
        if template_code is None:
            return

        # Look up template
        template = (
            db.query(NotificationTemplate)
            .filter(NotificationTemplate.code == template_code)
            .filter(NotificationTemplate.is_active.is_(True))
            .first()
        )

        if not template:
            logger.debug(
                f"No active notification template for code {template_code}"
            )
            return

        # Get recipient from event context
        recipient = self._resolve_recipient(db, event)
        if not recipient:
            logger.debug(
                f"Cannot determine recipient for event {event.event_type.value}"
            )
            return

        # Create notification
        # Include event context in the body for traceability
        body = self._render_body(template, event)

        notification = Notification(
            template_id=template.id,
            channel=template.channel or NotificationChannel.email,
            recipient=recipient,
            subject=self._render_subject(template, event),
            body=body,
            status=NotificationStatus.queued,
        )
        db.add(notification)

        logger.info(
            f"Queued notification for event {event.event_type.value} "
            f"to {recipient}"
        )

    def _resolve_recipient(self, db: Session, event: Event) -> str | None:
        """Resolve the notification recipient from event context."""
        # Try to get email from account
        if event.account_id:
            from app.models.subscriber import SubscriberAccount

            account = db.get(SubscriberAccount, event.account_id)
            if account and account.subscriber and account.subscriber.person:
                if account.subscriber.person.email:
                    return account.subscriber.person.email

        # Check if email is in payload
        if "email" in event.payload:
            return event.payload["email"]

        return None

    def _render_subject(self, template: NotificationTemplate, event: Event) -> str:
        """Render the notification subject with event data."""
        if not template.subject:
            return f"Notification: {event.event_type.value}"

        # Simple variable substitution
        subject = template.subject
        for key, value in event.payload.items():
            subject = subject.replace(f"{{{key}}}", str(value))
        return subject

    def _render_body(self, template: NotificationTemplate, event: Event) -> str:
        """Render the notification body with event data."""
        if not template.body:
            return f"Event: {event.event_type.value}\n{event.payload}"

        # Simple variable substitution
        body = template.body
        for key, value in event.payload.items():
            body = body.replace(f"{{{key}}}", str(value))
        return body
