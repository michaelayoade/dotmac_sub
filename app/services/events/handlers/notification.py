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
    # Network / OLT events
    EventType.ont_offline: "ont_offline",
    EventType.ont_online: "ont_online",
    EventType.ont_signal_degraded: "ont_signal_degraded",
    EventType.ont_discovered: "ont_discovered",
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
            logger.warning(
                "No active notification template for code %s", template_code
            )
            return

        # Get recipient from event context
        recipient = self._resolve_recipient(db, event)
        if not recipient:
            logger.debug(
                f"Cannot determine recipient for event {event.event_type.value}"
            )
            return

        # Build enriched context and render
        context = self._build_render_context(db, event)

        notification = Notification(
            template_id=template.id,
            channel=template.channel or NotificationChannel.email,
            recipient=recipient,
            subject=self._render_subject(template, event, context),
            body=self._render_body(template, event, context),
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
            from app.models.subscriber import Subscriber

            account = db.get(Subscriber, event.account_id)
            if account and account.email:
                return account.email

        # Check if email is in payload
        email = event.payload.get("email")
        if isinstance(email, str) and email:
            return email

        return None

    def _build_render_context(self, db: Session, event: Event) -> dict[str, str]:
        """Build variable substitution context from event payload + resolved data.

        Enriches the raw event payload with commonly needed variables
        that templates reference but events don't carry directly:
        subscriber_name, due_date, portal_url, device_serial, etc.
        """
        context: dict[str, str] = {}

        # Start with raw payload
        for key, value in event.payload.items():
            if value is not None:
                context[key] = str(value)

        # Resolve subscriber name from account_id
        if "subscriber_name" not in context and event.account_id:
            try:
                from app.models.subscriber import Subscriber

                subscriber = db.get(Subscriber, event.account_id)
                if subscriber:
                    name = subscriber.full_name or subscriber.display_name or ""
                    if not name and subscriber.first_name:
                        name = f"{subscriber.first_name} {subscriber.last_name or ''}".strip()
                    context["subscriber_name"] = name or "Valued Customer"
            except Exception:
                logger.warning(
                    "Failed to resolve subscriber name (account_id=%s)",
                    event.account_id,
                    exc_info=True,
                )
                context.setdefault("subscriber_name", "Valued Customer")

        # Resolve invoice details if invoice_id present
        if event.invoice_id and "invoice_number" not in context:
            try:
                from app.models.billing import Invoice

                invoice = db.get(Invoice, event.invoice_id)
                if invoice:
                    context.setdefault("invoice_number", invoice.invoice_number or "")
                    if invoice.total is not None:
                        context.setdefault("amount", f"₦{invoice.total:,.2f}")
                    if invoice.due_at:
                        context.setdefault("due_date", invoice.due_at.strftime("%b %d, %Y"))
            except Exception:
                logger.warning(
                    "Failed to resolve invoice details (invoice_id=%s)",
                    event.invoice_id,
                    exc_info=True,
                )

        # Normalize amount formatting
        if "amount" in context and not context["amount"].startswith("₦"):
            try:
                from decimal import Decimal, InvalidOperation

                amt = Decimal(context["amount"])
                context["amount"] = f"₦{amt:,.2f}"
            except (InvalidOperation, ValueError):
                pass

        # Map ONT payload keys to template variables
        context.setdefault("device_serial", context.get("serial_number", ""))

        # Usage percentage
        if "threshold" in context and "usage_percent" not in context:
            try:
                pct = float(context["threshold"]) * 100
                context["usage_percent"] = f"{pct:.0f}"
            except (ValueError, TypeError):
                pass

        # Portal URL (configurable, with sensible default)
        context.setdefault("portal_url", "/portal")

        # Location placeholder for ONT events
        context.setdefault("location", context.get("olt_name", ""))

        # Fallback for subscriber_name
        context.setdefault("subscriber_name", "Valued Customer")

        return context

    def _render_subject(self, template: NotificationTemplate, event: Event, context: dict[str, str]) -> str:
        """Render the notification subject with event data."""
        if not template.subject:
            return f"Notification: {event.event_type.value}"

        subject = template.subject
        for key, value in context.items():
            subject = subject.replace(f"{{{key}}}", value)
        return subject

    def _render_body(self, template: NotificationTemplate, event: Event, context: dict[str, str]) -> str:
        """Render the notification body with event data."""
        if not template.body:
            return f"Event: {event.event_type.value}\n{event.payload}"

        body = template.body
        for key, value in context.items():
            body = body.replace(f"{{{key}}}", value)
        return body
