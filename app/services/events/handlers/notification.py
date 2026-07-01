"""Notification handler for the event system."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationStatus,
    NotificationTemplate,
)
from app.services.customer_notification_policy import (
    has_recent_notification,
    is_notification_enabled_for_subscriber,
    quiet_hours_send_at,
    resolve_subscriber_id_for_recipient,
)
from app.services.events.types import Event, EventType

logger = logging.getLogger(__name__)
_LOGGED_MISSING_TEMPLATE_CODES: set[str] = set()

CHANNEL_TEMPLATE_SUFFIXES: dict[NotificationChannel, str] = {
    NotificationChannel.email: "email",
    NotificationChannel.sms: "sms",
    NotificationChannel.whatsapp: "whatsapp",
    NotificationChannel.push: "push",
    NotificationChannel.webhook: "webhook",
}

# Channel -> (setting key, env key) for the per-channel enable flag. A channel
# listed here is created in the fan-out only when its flag is truthy; channels
# NOT listed (e.g. email) are never suppressed at creation. We key off the
# explicit enable flag rather than provider `is_available()` because the latter
# is an unreliable signal (it false-negatives for email even while email
# delivers), so gating creation on it would drop every notification.
_CHANNEL_ENABLE_FLAG: dict[NotificationChannel, tuple[str, str]] = {
    NotificationChannel.sms: ("sms_enabled", "SMS_ENABLED"),
}

_DISABLED_VALUES = {"false", "0", "no", "off", "disabled"}
_ENABLED_VALUES = {"1", "true", "yes", "on", "enabled"}
_UNRESOLVED_TEMPLATE_RE = re.compile(r"\{\{?\s*[a-zA-Z0-9_]+\s*\}?\}")


def _notification_setting_value(db: Session, key: str) -> str | None:
    setting = (
        db.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.notification)
        .filter(DomainSetting.key == key)
        .filter(DomainSetting.is_active.is_(True))
        .first()
    )
    if not setting:
        return None
    if setting.value_text is not None:
        return str(setting.value_text)
    if setting.value_json is not None:
        return str(setting.value_json)
    return None


def _event_enabled(db: Session, template_code: str) -> bool:
    value = _notification_setting_value(db, f"notification_event_{template_code}_enabled")
    if value is None:
        return True
    return value.strip().lower() in _ENABLED_VALUES


def _event_channels(
    db: Session,
    template_code: str,
    default_channels: tuple[NotificationChannel, ...],
) -> tuple[NotificationChannel, ...]:
    value = _notification_setting_value(db, f"notification_event_{template_code}_channels")
    if not value:
        return default_channels
    channels: list[NotificationChannel] = []
    for item in value.split(","):
        raw = item.strip().lower()
        if not raw:
            continue
        try:
            channel = NotificationChannel(raw)
        except ValueError:
            logger.warning(
                "Ignoring invalid notification channel %r for event %s",
                raw,
                template_code,
            )
            continue
        if channel not in channels:
            channels.append(channel)
    return tuple(channels) or default_channels


def _channel_disabled_in_config(db: Session, channel: NotificationChannel) -> bool:
    """True only if ``channel`` is explicitly disabled by its enable flag.

    Fail-open: any channel without a configured enable flag returns False (never
    suppressed). Reads the same setting the provider uses, on the caller's
    session, so it reflects live config without opening a new transaction.
    """
    flag = _CHANNEL_ENABLE_FLAG.get(channel)
    if not flag:
        return False
    try:
        from app.services.sms import _get_setting

        value = _get_setting(db, flag[0], flag[1], "true")
    except Exception:
        # Never suppress on a lookup error — better a row that may fail at
        # dispatch than a silently dropped notification.
        return False
    return str(value or "true").strip().lower() in _DISABLED_VALUES


@dataclass(frozen=True)
class EventNotificationSpec:
    template_code: str
    category: str
    channels: tuple[NotificationChannel, ...]
    subject: str
    body: str


EVENT_NOTIFICATION_SPECS: dict[EventType, EventNotificationSpec] = {
    EventType.subscriber_created: EventNotificationSpec(
        template_code="subscriber_created",
        category="account",
        channels=(NotificationChannel.email,),
        subject="Your customer account is ready",
        body=(
            "Dear {subscriber_name},\n\n"
            "Your customer account has been created successfully. "
            "You can now manage your services and billing through the portal.\n\n"
            "Thank you for choosing us."
        ),
    ),
    EventType.subscriber_updated: EventNotificationSpec(
        template_code="subscriber_updated",
        category="account",
        channels=(NotificationChannel.email,),
        subject="Your account profile was updated",
        body=(
            "Dear {subscriber_name},\n\n"
            "Your account profile was updated successfully.\n\n"
            "Updated fields: {updated_fields}\n\n"
            "If you did not make this change, please contact support immediately."
        ),
    ),
    EventType.subscription_created: EventNotificationSpec(
        template_code="subscription_created",
        category="service",
        channels=(NotificationChannel.email, NotificationChannel.sms),
        subject="Your new service subscription",
        body=(
            "Dear {subscriber_name},\n\n"
            "Your subscription to {offer_name} has been created. "
            "A service order will be created for installation.\n\n"
            "If you have questions, contact our support team."
        ),
    ),
    EventType.subscription_activated: EventNotificationSpec(
        template_code="subscription_activated",
        category="service",
        channels=(NotificationChannel.email, NotificationChannel.sms),
        subject="Your service is now active",
        body=(
            "Dear {subscriber_name},\n\n"
            "Your {offer_name} subscription is now active and ready to use."
        ),
    ),
    EventType.subscription_suspended: EventNotificationSpec(
        template_code="subscription_suspended",
        category="service",
        channels=(NotificationChannel.email, NotificationChannel.sms),
        subject="Service suspended",
        body=(
            "Dear {subscriber_name},\n\n"
            "Your {offer_name} subscription has been suspended. "
            "Please make payment or contact support to restore service."
        ),
    ),
    EventType.subscription_resumed: EventNotificationSpec(
        template_code="subscription_resumed",
        category="service",
        channels=(NotificationChannel.email, NotificationChannel.sms),
        subject="Your service has been resumed",
        body=(
            "Dear {subscriber_name},\n\n"
            "Your {offer_name} subscription has been resumed successfully."
        ),
    ),
    EventType.subscription_canceled: EventNotificationSpec(
        template_code="subscription_canceled",
        category="service",
        channels=(NotificationChannel.email,),
        subject="Subscription canceled",
        body=(
            "Dear {subscriber_name},\n\n"
            "Your {offer_name} subscription has been canceled. "
            "If this was unexpected, please contact support."
        ),
    ),
    EventType.subscription_expiring: EventNotificationSpec(
        template_code="subscription_expiring",
        category="service",
        channels=(NotificationChannel.email, NotificationChannel.sms),
        subject="Your subscription is expiring soon",
        body=(
            "Dear {subscriber_name},\n\n"
            "Your {offer_name} subscription will expire soon. "
            "Please renew to avoid interruption."
        ),
    ),
    EventType.subscription_expired: EventNotificationSpec(
        template_code="subscription_expired",
        category="service",
        channels=(NotificationChannel.email, NotificationChannel.sms),
        subject="Your subscription has expired",
        body=(
            "Dear {subscriber_name},\n\n"
            "Your {offer_name} subscription has expired. "
            "Renew your service to restore access."
        ),
    ),
    EventType.subscription_suspension_warning: EventNotificationSpec(
        template_code="suspension_warning",
        category="billing",
        channels=(NotificationChannel.email, NotificationChannel.sms),
        subject="A reminder about invoice #{invoice_number}",
        body=(
            "Dear {subscriber_name},\n\n"
            "Invoice #{invoice_number} for {amount} is currently unpaid. To avoid "
            "any interruption to your service, please arrange payment when you "
            "can. If you have already paid, please disregard this message."
        ),
    ),
    EventType.subscription_upgraded: EventNotificationSpec(
        template_code="subscription_upgraded",
        category="service",
        channels=(NotificationChannel.email, NotificationChannel.sms),
        subject="Your plan has been upgraded",
        body=(
            "Dear {subscriber_name},\n\n"
            "Your service has been upgraded from {old_offer_name} to {new_offer_name}."
        ),
    ),
    EventType.subscription_downgraded: EventNotificationSpec(
        template_code="subscription_downgraded",
        category="service",
        channels=(NotificationChannel.email, NotificationChannel.sms),
        subject="Your plan has been updated",
        body=(
            "Dear {subscriber_name},\n\n"
            "Your service has been changed from {old_offer_name} to {new_offer_name}."
        ),
    ),
    EventType.invoice_created: EventNotificationSpec(
        template_code="invoice_created",
        category="billing",
        channels=(NotificationChannel.email,),
        subject="New invoice #{invoice_number}",
        body=(
            "Dear {subscriber_name},\n\n"
            "A new invoice #{invoice_number} for {amount} has been generated. "
            "Due date: {due_date}."
        ),
    ),
    EventType.invoice_sent: EventNotificationSpec(
        template_code="invoice_sent",
        category="billing",
        channels=(NotificationChannel.email, NotificationChannel.sms),
        subject="Invoice #{invoice_number} — payment due {due_date}",
        body=(
            "Dear {subscriber_name},\n\n"
            "Invoice #{invoice_number} for {amount} is due on {due_date}. "
            "Please pay before the due date to avoid disruption."
        ),
    ),
    EventType.invoice_paid: EventNotificationSpec(
        template_code="invoice_paid",
        category="billing",
        channels=(NotificationChannel.email,),
        subject="Invoice #{invoice_number} has been paid",
        body=(
            "Dear {subscriber_name},\n\n"
            "Invoice #{invoice_number} has been paid successfully. "
            "Thank you for your payment."
        ),
    ),
    EventType.invoice_overdue: EventNotificationSpec(
        template_code="invoice_overdue",
        category="billing",
        channels=(NotificationChannel.email, NotificationChannel.sms),
        subject="Invoice #{invoice_number} is now due",
        body=(
            "Dear {subscriber_name},\n\n"
            "Our records show invoice #{invoice_number} for {amount} is now past "
            "its due date. If you have already paid, please disregard this "
            "message. Otherwise you can pay anytime at {portal_url}/billing."
        ),
    ),
    EventType.payment_received: EventNotificationSpec(
        template_code="payment_received",
        category="billing",
        channels=(NotificationChannel.email, NotificationChannel.sms),
        subject="Payment received — thank you",
        body=(
            "Dear {subscriber_name},\n\n"
            "We have received your payment of {amount}. Thank you."
        ),
    ),
    EventType.payment_failed: EventNotificationSpec(
        template_code="payment_failed",
        category="billing",
        channels=(NotificationChannel.email,),
        subject="Payment failed — please retry",
        body=(
            "Dear {subscriber_name},\n\n"
            "Your recent payment attempt of {amount} was not successful. "
            "Please try again or use a different payment method."
        ),
    ),
    EventType.payment_refunded: EventNotificationSpec(
        template_code="payment_refunded",
        category="billing",
        channels=(NotificationChannel.email,),
        subject="Payment refunded",
        body=(
            "Dear {subscriber_name},\n\n"
            "A refund of {amount} has been processed on your account."
        ),
    ),
    EventType.arrangement_defaulted: EventNotificationSpec(
        template_code="arrangement_defaulted",
        category="billing",
        channels=(NotificationChannel.email, NotificationChannel.sms),
        subject="Update on your payment arrangement",
        body=(
            "Dear {subscriber_name},\n\n"
            "It looks like your payment arrangement has fallen behind, and the "
            "outstanding balance of {total_amount} is now due. If you've already "
            "paid, please disregard this message. Otherwise, please contact our "
            "billing team and we'll be happy to help."
        ),
    ),
    EventType.usage_warning: EventNotificationSpec(
        template_code="usage_warning",
        category="usage",
        channels=(NotificationChannel.push, NotificationChannel.email),
        subject="Data usage warning — {usage_percent}% used",
        body=(
            "Dear {subscriber_name},\n\n"
            "You have used {usage_percent}% of your monthly data allowance on {offer_name}."
        ),
    ),
    EventType.usage_exhausted: EventNotificationSpec(
        template_code="usage_exhausted",
        category="usage",
        channels=(NotificationChannel.push, NotificationChannel.email),
        subject="Data allowance exhausted",
        body=(
            "Dear {subscriber_name},\n\n"
            "Your monthly data allowance on {offer_name} has been exhausted."
        ),
    ),
    EventType.service_extended: EventNotificationSpec(
        template_code="service_extended",
        category="billing",
        channels=(NotificationChannel.push, NotificationChannel.email),
        subject="Your service has been extended",
        body=(
            "Dear {subscriber_name},\n\n"
            "We have added {days} day(s) to your service as compensation: "
            "{reason}. Your service now runs until {extended_until}."
        ),
    ),
    EventType.addon_expiring: EventNotificationSpec(
        template_code="addon_expiring",
        category="usage",
        channels=(NotificationChannel.push, NotificationChannel.email),
        subject="Your data bundle expires soon",
        body=(
            "Dear {subscriber_name},\n\n"
            "Your {addon_name} data bundle expires on {expires_at}. "
            "Unused data lapses with it — top up again to stay connected."
        ),
    ),
    EventType.usage_topped_up: EventNotificationSpec(
        template_code="usage_topped_up",
        category="usage",
        channels=(NotificationChannel.email, NotificationChannel.sms),
        subject="Top-up received",
        body=(
            "Dear {subscriber_name},\n\n"
            "Your account has been topped up with {amount}. Reference: {reference}."
        ),
    ),
    EventType.provisioning_completed: EventNotificationSpec(
        template_code="provisioning_completed",
        category="service",
        channels=(NotificationChannel.email, NotificationChannel.sms),
        subject="Service installation complete",
        body=(
            "Dear {subscriber_name},\n\n"
            "Your service installation has been completed successfully."
        ),
    ),
    EventType.provisioning_failed: EventNotificationSpec(
        template_code="provisioning_failed",
        category="service",
        channels=(NotificationChannel.email,),
        subject="Service installation issue",
        body=(
            "Dear {subscriber_name},\n\n"
            "We encountered an issue while setting up your service. "
            "Our technical team will follow up shortly."
        ),
    ),
    EventType.service_order_created: EventNotificationSpec(
        template_code="service_order_created",
        category="service",
        channels=(NotificationChannel.email, NotificationChannel.sms),
        subject="Your service order has been created",
        body=(
            "Dear {subscriber_name},\n\n"
            "Service order #{service_order_id} has been created for your account."
        ),
    ),
    EventType.service_order_assigned: EventNotificationSpec(
        template_code="service_order_assigned",
        category="service",
        channels=(NotificationChannel.email, NotificationChannel.sms),
        subject="Your service order is in progress",
        body=(
            "Dear {subscriber_name},\n\n"
            "Service order #{service_order_id} is now being worked on by our team."
        ),
    ),
    EventType.service_order_completed: EventNotificationSpec(
        template_code="service_order_completed",
        category="service",
        channels=(NotificationChannel.email, NotificationChannel.sms),
        subject="Your service order is complete",
        body=(
            "Dear {subscriber_name},\n\n"
            "Service order #{service_order_id} has been completed successfully."
        ),
    ),
    EventType.ont_offline: EventNotificationSpec(
        template_code="ont_offline",
        category="service",
        channels=(NotificationChannel.email,),
        subject="We've noticed an issue with your connection",
        body=(
            "Dear {subscriber_name},\n\n"
            "We've detected that your service may currently be offline, and our "
            "team is looking into it. If your equipment has lost power, please "
            "check that it is switched on. If the issue continues, please contact "
            "our support team."
        ),
    ),
    EventType.ont_online: EventNotificationSpec(
        template_code="ont_online",
        category="service",
        channels=(NotificationChannel.email,),
        subject="Your connection is back online",
        body=(
            "Dear {subscriber_name},\n\n"
            "Good news — your service is back online. Thank you for your "
            "patience. If you continue to experience any issues, please contact "
            "our support team."
        ),
    ),
    EventType.ont_signal_degraded: EventNotificationSpec(
        template_code="ont_signal_degraded",
        category="service",
        channels=(NotificationChannel.email,),
        subject="We're checking on your connection quality",
        body=(
            "Dear {subscriber_name},\n\n"
            "Our monitoring suggests your connection quality may be reduced, and "
            "our team is looking into it. There is nothing you need to do right "
            "now. If you notice any problems, please contact our support team."
        ),
    ),
    EventType.ont_discovered: EventNotificationSpec(
        template_code="ont_discovered",
        category="service",
        channels=(NotificationChannel.email,),
        subject="New ONT discovered — {device_serial}",
        body="A new ONT has been discovered on the network (internal notification).",
    ),
}

EVENT_TYPE_TO_TEMPLATE = {
    event_type: spec.template_code
    for event_type, spec in EVENT_NOTIFICATION_SPECS.items()
}

BALANCE_NOTIFICATION_EVENTS: set[EventType] = {
    EventType.invoice_created,
    EventType.invoice_sent,
    EventType.invoice_overdue,
    EventType.subscription_suspension_warning,
    EventType.arrangement_defaulted,
}

BILLING_SUSPENSION_REASONS = {"overdue", "dunning", "invoice_overdue"}


class NotificationHandler:
    """Handler that queues customer notifications."""

    def handle(self, db: Session, event: Event) -> None:
        spec = EVENT_NOTIFICATION_SPECS.get(event.event_type)
        if spec is None:
            return
        if not _event_enabled(db, spec.template_code):
            logger.info(
                "Suppressed notification for event %s: event disabled by settings",
                event.event_type.value,
            )
            return

        # Back-office bookkeeping (e.g. the cutover credit reconcile) suppresses
        # customer notifications: the activity is not a real-time customer action,
        # so "Payment received"/"Service resumed" mail would be wrong and, in a
        # bulk burst to churned mailboxes, reputation-damaging.
        from app.services.notification_suppression import notifications_suppressed

        if notifications_suppressed():
            logger.info(
                "Suppressed notification for event %s (back-office scope)",
                event.event_type.value,
            )
            return

        if self._customer_balance_notifications_suppressed(db, event):
            logger.info(
                "Suppressed customer balance notification for event %s",
                event.event_type.value,
            )
            return

        templates = self._load_templates(db, spec.template_code)
        if not templates:
            templates = self._seed_and_reload_templates(db, spec.template_code)
        if not templates and spec.template_code not in _LOGGED_MISSING_TEMPLATE_CODES:
            logger.warning(
                "No active notification template for code %s", spec.template_code
            )
            _LOGGED_MISSING_TEMPLATE_CODES.add(spec.template_code)

        context = self._build_render_context(db, event)
        quiet_send_at = quiet_hours_send_at(db)
        templates_by_channel = {
            (template.channel or NotificationChannel.email): template
            for template in templates
        }
        for channel in _event_channels(db, spec.template_code, spec.channels):
            # Skip channels explicitly disabled in config, so we don't create a
            # row per spec channel that can only fail at dispatch (e.g. SMS with
            # sms_enabled=false / no provider — the source of the failed-SMS
            # backlog). This checks the channel ENABLE FLAG (deterministic
            # config intent), NOT provider reachability: `is_available()` is an
            # unreliable signal here — it false-negatives for email even while
            # email is delivering, so gating creation on it would suppress every
            # notification. Fail-open: channels with no enable flag are never
            # skipped here; transient send failures stay the dispatcher's job.
            if _channel_disabled_in_config(db, channel):
                logger.debug(
                    "Skipping event %s on %s: channel disabled in config",
                    event.event_type.value,
                    channel.value,
                )
                continue
            template = templates_by_channel.get(channel)
            if template is None:
                logger.info(
                    "Suppressed notification for event %s on %s: no active template for code %s",
                    event.event_type.value,
                    channel.value,
                    spec.template_code,
                )
                continue
            recipient = self._resolve_recipient(db, event, channel)
            if not recipient:
                logger.debug(
                    "Cannot determine recipient for event %s on channel %s",
                    event.event_type.value,
                    channel.value,
                )
                continue

            subscriber_id = self._resolve_subscriber_id(db, event, recipient)
            # Hard account-status gate (overrides preferences): terminal accounts
            # (canceled/disabled) get nothing; walled accounts (suspended/blocked)
            # get only actionable categories. Never mail a churned/closed account.
            if subscriber_id and not self._status_allows(
                db, subscriber_id, spec.category
            ):
                logger.info(
                    "Suppressed %s notification for event %s on %s to %s by account status",
                    spec.category,
                    event.event_type.value,
                    channel.value,
                    recipient,
                )
                continue
            if not is_notification_enabled_for_subscriber(
                db,
                subscriber_id=subscriber_id,
                channel=channel,
                category=spec.category,
                recipient=recipient,
            ):
                logger.info(
                    "Suppressed notification for event %s on %s to %s by preferences",
                    event.event_type.value,
                    channel.value,
                    recipient,
                )
                continue
            if has_recent_notification(
                db,
                subscriber_id=subscriber_id,
                channel=channel,
                event_type=spec.template_code,
                category=spec.category,
                recipient=recipient,
            ):
                logger.info(
                    "Suppressed duplicate notification for event %s on %s to %s",
                    event.event_type.value,
                    channel.value,
                    recipient,
                )
                continue

            subject = self._render_subject(template, spec, context)
            body = self._render_body(template, spec, context)
            unresolved = sorted(
                {
                    *_UNRESOLVED_TEMPLATE_RE.findall(subject),
                    *_UNRESOLVED_TEMPLATE_RE.findall(body),
                }
            )
            if unresolved:
                logger.error(
                    "Suppressed notification for event %s on %s: unresolved template variable(s) %s",
                    event.event_type.value,
                    channel.value,
                    ", ".join(unresolved),
                )
                continue

            notification = Notification(
                template_id=template.id if template else None,
                subscriber_id=subscriber_id,
                channel=channel,
                event_type=spec.template_code,
                category=spec.category,
                recipient=recipient,
                subject=subject,
                body=body,
                status=NotificationStatus.queued,
                send_at=quiet_send_at,
            )
            db.add(notification)

            logger.info(
                "Queued notification for event %s on %s to %s",
                event.event_type.value,
                channel.value,
                recipient,
            )

    def _status_allows(self, db: Session, subscriber_id, category: str) -> bool:
        """Apply the account-status notification gate (kill-switch aware)."""
        from app.models.domain_settings import SettingDomain
        from app.models.subscriber import Subscriber
        from app.services import settings_spec
        from app.services.notification_status_policy import status_allows_notification

        enabled = settings_spec.resolve_value(
            db, SettingDomain.notification, "status_gate_enabled"
        )
        if enabled is False:
            return True
        subscriber = db.get(Subscriber, subscriber_id)
        status = subscriber.status if subscriber else None
        return status_allows_notification(status, category)

    def _customer_balance_notifications_suppressed(
        self, db: Session, event: Event
    ) -> bool:
        """Suppress customer-facing balance/debt notifications when disabled."""
        from app.models.domain_settings import SettingDomain
        from app.services import settings_spec

        enabled = settings_spec.resolve_value(
            db, SettingDomain.billing, "customer_balance_notifications_enabled"
        )
        if enabled is not False and str(enabled).lower() not in {
            "0",
            "false",
            "no",
            "off",
            "",
        }:
            return False

        if event.event_type in BALANCE_NOTIFICATION_EVENTS:
            return True
        return self._is_billing_suspension_event(event)

    def _is_billing_suspension_event(self, event: Event) -> bool:
        if event.event_type != EventType.subscription_suspended:
            return False

        reason = str(event.payload.get("reason") or "").strip().lower()
        source = str(event.payload.get("source") or "").strip().lower()
        return (
            reason in BILLING_SUSPENSION_REASONS
            or source in BILLING_SUSPENSION_REASONS
            or source.startswith("invoice:")
        )

    def _load_templates(
        self,
        db: Session,
        template_code: str,
    ) -> list[NotificationTemplate]:
        candidate_codes = {
            template_code,
            *(
                f"{template_code}_{suffix}"
                for suffix in CHANNEL_TEMPLATE_SUFFIXES.values()
            ),
        }
        templates = list(
            db.scalars(
                select(NotificationTemplate)
                .where(NotificationTemplate.code.in_(candidate_codes))
                .where(NotificationTemplate.is_active.is_(True))
            ).all()
        )
        return self._order_templates(templates, template_code)

    def _seed_and_reload_templates(
        self,
        db: Session,
        template_code: str,
    ) -> list[NotificationTemplate]:
        try:
            from app.services.settings_seed import _seed_missing_notification_templates

            _seed_missing_notification_templates(db)
        except Exception:
            logger.debug(
                "Notification template reseed failed for code %s",
                template_code,
                exc_info=True,
            )
        return self._load_templates(db, template_code)

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

    def _resolve_subscriber_id(
        self,
        db: Session,
        event: Event,
        recipient: str | None,
    ):
        if event.account_id:
            return event.account_id
        if event.subscriber_id:
            return event.subscriber_id
        return resolve_subscriber_id_for_recipient(db, recipient)

    def _resolve_recipient(
        self,
        db: Session,
        event: Event,
        channel: NotificationChannel,
    ) -> str | None:
        email = event.payload.get("email")
        phone = event.payload.get("phone") or event.payload.get("phone_number")
        subscriber_id = event.account_id or event.subscriber_id

        if subscriber_id:
            from app.models.subscriber import Subscriber

            subscriber = db.get(Subscriber, subscriber_id)
            if channel == NotificationChannel.email and subscriber and subscriber.email:
                return subscriber.email
            # Push delivery targets the subscriber's device tokens; the
            # recipient string is only the in-app record's address field.
            if channel == NotificationChannel.push and subscriber and subscriber.email:
                return subscriber.email
            if (
                channel in {NotificationChannel.sms, NotificationChannel.whatsapp}
                and subscriber
                and subscriber.phone
            ):
                return subscriber.phone

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
        context: dict[str, str] = {}
        for key, value in event.payload.items():
            if value is not None:
                context[key] = str(value)

        if "subscriber_name" not in context:
            subscriber_id = event.account_id or event.subscriber_id
            if subscriber_id:
                try:
                    from app.models.subscriber import Subscriber

                    subscriber = db.get(Subscriber, subscriber_id)
                    if subscriber:
                        context["subscriber_name"] = (
                            subscriber.name or "Valued Customer"
                        )
                except Exception:
                    logger.warning(
                        "Failed to resolve subscriber name (subscriber_id=%s)",
                        subscriber_id,
                        exc_info=True,
                    )

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

        if event.subscription_id:
            try:
                from app.models.catalog import CatalogOffer, Subscription

                subscription = db.get(Subscription, event.subscription_id)
                if subscription:
                    offer = db.get(CatalogOffer, subscription.offer_id)
                    if offer:
                        context.setdefault("offer_name", offer.name or "")
                        context.setdefault("plan_name", offer.name or "")
            except Exception:
                logger.warning(
                    "Failed to resolve subscription details (subscription_id=%s)",
                    event.subscription_id,
                    exc_info=True,
                )

        if event.service_order_id:
            context.setdefault("service_order_id", str(event.service_order_id))
        elif "service_order_id" in context:
            context["service_order_id"] = str(context["service_order_id"])

        if "old_offer" in context:
            context.setdefault("old_offer_name", context["old_offer"])
        if "new_offer" in context:
            context.setdefault("new_offer_name", context["new_offer"])

        if "updated_fields" in context:
            context["updated_fields"] = context["updated_fields"].strip("[]")

        if "amount" in context and not context["amount"].startswith("₦"):
            try:
                from decimal import Decimal, InvalidOperation

                amount = Decimal(context["amount"])
                context["amount"] = f"₦{amount:,.2f}"
            except (InvalidOperation, ValueError):
                pass

        context.setdefault("device_serial", context.get("serial_number", ""))
        context.setdefault("location", context.get("olt_name", ""))
        context.setdefault("portal_url", "/portal")
        context.setdefault("subscriber_name", "Valued Customer")
        context.setdefault("offer_name", context.get("plan_name", "your service"))
        context.setdefault("old_offer_name", "your current plan")
        context.setdefault("new_offer_name", "your updated plan")
        context.setdefault("updated_fields", "profile details")
        return context

    def _render_text(self, text: str, context: dict[str, str]) -> str:
        rendered = text
        for key, value in context.items():
            rendered = rendered.replace(f"{{{key}}}", value)
        return rendered

    def _render_subject(
        self,
        template: NotificationTemplate | None,
        spec: EventNotificationSpec,
        context: dict[str, str],
    ) -> str:
        return self._render_text(
            (template.subject if template and template.subject else spec.subject),
            context,
        )

    def _render_body(
        self,
        template: NotificationTemplate | None,
        spec: EventNotificationSpec,
        context: dict[str, str],
    ) -> str:
        return self._render_text(
            (template.body if template and template.body else spec.body), context
        )
