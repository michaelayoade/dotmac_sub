"""Notification handler for the event system.

Queues customer notifications based on configured notification templates.
"""

import logging
from collections.abc import Iterable

from sqlalchemy.orm import Session

from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationStatus,
    NotificationTemplate,
)
from app.services.events.types import Event, EventType

logger = logging.getLogger(__name__)

CHANNEL_TEMPLATE_SUFFIXES: dict[NotificationChannel, str] = {
    NotificationChannel.email: "email",
    NotificationChannel.sms: "sms",
    NotificationChannel.whatsapp: "whatsapp",
    NotificationChannel.push: "push",
    NotificationChannel.webhook: "webhook",
}


# Mapping from EventType to notification template codes
# These codes are used to look up templates in the notification_templates table
EVENT_TYPE_TO_TEMPLATE = {
    # Subscription events
    EventType.subscription_created: "subscription_created",
    EventType.subscription_activated: "subscription_activated",
    EventType.subscription_suspended: "subscription_suspended",
    EventType.subscription_canceled: "subscription_canceled",
    EventType.subscription_expiring: "subscription_expiring",
    EventType.subscription_suspension_warning: "suspension_warning",
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

        templates = self._load_templates(db, template_code)
        if not templates:
            logger.warning("No active notification template for code %s", template_code)
            return

        # Build enriched context and render
        context = self._build_render_context(db, event)

        for template in templates:
            channel = template.channel or NotificationChannel.email
            recipient = self._resolve_recipient(db, event, channel)
            if not recipient:
                logger.debug(
                    "Cannot determine recipient for event %s on channel %s",
                    event.event_type.value,
                    channel.value,
                )
                continue

            notification = Notification(
                template_id=template.id,
                channel=channel,
                recipient=recipient,
                subject=self._render_subject(template, event, context),
                body=self._render_body(template, event, context),
                status=NotificationStatus.queued,
            )
            db.add(notification)

            logger.info(
                "Queued notification for event %s on %s to %s",
                event.event_type.value,
                channel.value,
                recipient,
            )

    def _load_templates(
        self,
        db: Session,
        template_code: str,
    ) -> list[NotificationTemplate]:
        """Load active templates for the event's base code and channel variants."""
        candidate_codes = {
            template_code,
            *(
                f"{template_code}_{suffix}"
                for suffix in CHANNEL_TEMPLATE_SUFFIXES.values()
            ),
        }
        templates = (
            db.query(NotificationTemplate)
            .filter(NotificationTemplate.code.in_(candidate_codes))
            .filter(NotificationTemplate.is_active.is_(True))
            .all()
        )
        return self._order_templates(templates, template_code)

    def _order_templates(
        self,
        templates: Iterable[NotificationTemplate],
        template_code: str,
    ) -> list[NotificationTemplate]:
        suffix_to_channel = {
            suffix: channel for channel, suffix in CHANNEL_TEMPLATE_SUFFIXES.items()
        }
        ordered: dict[str, NotificationTemplate] = {}

        for template in templates:
            channel = template.channel or NotificationChannel.email
            expected_code = f"{template_code}_{CHANNEL_TEMPLATE_SUFFIXES[channel]}"
            if template.code == template_code:
                ordered[channel.value] = template
                continue
            if template.code == expected_code:
                ordered[channel.value] = template
                continue
            suffix = template.code.removeprefix(f"{template_code}_")
            mapped_channel = suffix_to_channel.get(suffix)
            if mapped_channel and mapped_channel.value not in ordered:
                ordered[mapped_channel.value] = template

        return list(ordered.values())

    def _resolve_recipient(
        self,
        db: Session,
        event: Event,
        channel: NotificationChannel,
    ) -> str | None:
        """Resolve the notification recipient from event context."""
        email = event.payload.get("email")
        phone = event.payload.get("phone") or event.payload.get("phone_number")

        if event.account_id:
            from app.models.subscriber import Subscriber

            account = db.get(Subscriber, event.account_id)
            if channel == NotificationChannel.email and account and account.email:
                return account.email
            if (
                channel in {NotificationChannel.sms, NotificationChannel.whatsapp}
                and account
                and account.phone
            ):
                return account.phone

        if channel == NotificationChannel.email and isinstance(email, str) and email:
            return email
        if (
            channel in {NotificationChannel.sms, NotificationChannel.whatsapp}
            and isinstance(phone, str)
            and phone
        ):
            return phone

        if isinstance(email, str) and email:
            return email
        if isinstance(phone, str) and phone:
            return phone

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
                        context.setdefault(
                            "due_date", invoice.due_at.strftime("%b %d, %Y")
                        )
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

    def _render_subject(
        self, template: NotificationTemplate, event: Event, context: dict[str, str]
    ) -> str:
        """Render the notification subject with event data."""
        if not template.subject:
            return f"Notification: {event.event_type.value}"

        subject = template.subject
        for key, value in context.items():
            subject = subject.replace(f"{{{key}}}", value)
        return subject

    def _render_body(
        self, template: NotificationTemplate, event: Event, context: dict[str, str]
    ) -> str:
        """Render the notification body with event data."""
        if not template.body:
            return f"Event: {event.event_type.value}\n{event.payload}"

        body = template.body
        for key, value in context.items():
            body = body.replace(f"{{{key}}}", value)
        return body
