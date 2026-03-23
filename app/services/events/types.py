"""Event types and data structures for the event system.

This module defines all event types used throughout the application,
plus the Event dataclass that encapsulates event data.
"""

import enum
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

logger = logging.getLogger(__name__)


class EventType(enum.Enum):
    """All event types supported by the event system (~40 events).

    Event naming convention: {entity}.{action}
    """

    # Subscriber events (5)
    subscriber_created = "subscriber.created"
    subscriber_updated = "subscriber.updated"
    subscriber_suspended = "subscriber.suspended"
    subscriber_reactivated = "subscriber.reactivated"
    subscriber_throttled = "subscriber.throttled"

    # Subscription events (8)
    subscription_created = "subscription.created"
    subscription_activated = "subscription.activated"
    subscription_suspended = "subscription.suspended"
    subscription_resumed = "subscription.resumed"
    subscription_canceled = "subscription.canceled"
    subscription_upgraded = "subscription.upgraded"
    subscription_downgraded = "subscription.downgraded"
    subscription_expiring = "subscription.expiring"
    subscription_expired = "subscription.expired"
    subscription_suspension_warning = "subscription.suspension_warning"
    subscription_deleted = "subscription.deleted"

    # Billing - Invoice events (4)
    invoice_created = "invoice.created"
    invoice_sent = "invoice.sent"
    invoice_paid = "invoice.paid"
    invoice_overdue = "invoice.overdue"

    # Billing - Payment events (3)
    payment_received = "payment.received"
    payment_failed = "payment.failed"
    payment_refunded = "payment.refunded"

    # Usage events (4)
    usage_recorded = "usage.recorded"
    usage_warning = "usage.warning"
    usage_exhausted = "usage.exhausted"
    usage_topped_up = "usage.topped_up"

    # Operations - Provisioning events (3)
    provisioning_started = "provisioning.started"
    provisioning_completed = "provisioning.completed"
    provisioning_failed = "provisioning.failed"

    # Operations - Service Order events (3)
    service_order_created = "service_order.created"
    service_order_assigned = "service_order.assigned"
    service_order_completed = "service_order.completed"

    # Operations - Appointment events (2)
    appointment_scheduled = "appointment.scheduled"
    appointment_missed = "appointment.missed"

    # Network events (4)
    device_offline = "device.offline"
    device_online = "device.online"
    session_started = "session.started"
    session_ended = "session.ended"

    # OLT events (3)
    olt_created = "olt.created"
    olt_updated = "olt.updated"
    olt_deleted = "olt.deleted"

    # ONT events (5)
    ont_discovered = "ont.discovered"
    ont_online = "ont.online"
    ont_offline = "ont.offline"
    ont_signal_degraded = "ont.signal_degraded"
    ont_signal_delta = "ont.signal_delta"
    ont_provisioned = "ont.provisioned"
    ont_config_updated = "ont.config_updated"
    ont_moved = "ont.moved"
    ont_feature_toggled = "ont.feature_toggled"

    # Collections - Dunning events (4)
    dunning_started = "dunning.started"
    dunning_action_executed = "dunning.action_executed"
    dunning_resolved = "dunning.resolved"
    dunning_paused = "dunning.paused"

    # Enforcement locks (2)
    enforcement_lock_created = "enforcement_lock.created"
    enforcement_lock_resolved = "enforcement_lock.resolved"

    # Network alert (legacy, kept for compatibility)
    network_alert = "network.alert"

    # Customer portal events (4)
    customer_login = "customer.login"
    customer_logout = "customer.logout"
    customer_ticket_created = "customer.ticket_created"
    customer_password_changed = "customer.password_changed"  # noqa: S105 - event name, not a password

    # Reseller events (3)
    reseller_login = "reseller.login"
    reseller_logout = "reseller.logout"
    reseller_impersonated = "reseller.impersonated"

    # NAS events (7)
    nas_device_created = "nas_device.created"
    nas_device_updated = "nas_device.updated"
    nas_device_deleted = "nas_device.deleted"
    nas_backup_completed = "nas_backup.completed"
    nas_backup_failed = "nas_backup.failed"
    nas_provisioning_completed = "nas_provisioning.completed"
    nas_provisioning_failed = "nas_provisioning.failed"

    # TR-069 events (4)
    tr069_job_completed = "tr069_job.completed"
    tr069_job_failed = "tr069_job.failed"
    tr069_device_discovered = "tr069_device.discovered"
    tr069_device_stale = "tr069_device.stale"

    # Custom event type for extensibility
    custom = "custom"


# Mapping from EventType to LifecycleEventType for subscription events
SUBSCRIPTION_LIFECYCLE_MAP = {
    EventType.subscription_activated: "activate",
    EventType.subscription_suspended: "suspend",
    EventType.subscription_resumed: "resume",
    EventType.subscription_canceled: "cancel",
    EventType.subscription_upgraded: "upgrade",
    EventType.subscription_downgraded: "downgrade",
}


@dataclass
class Event:
    """Represents an event that occurred in the system.

    This is the central data structure passed through the event system.
    It contains all information needed by handlers to process the event.
    """

    event_type: EventType
    payload: dict[str, Any]
    event_id: UUID = field(default_factory=uuid4)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    # Context fields - optional, used for routing and filtering
    actor: str | None = None  # Who triggered the event (user ID, system, etc.)
    subscriber_id: UUID | None = None
    account_id: UUID | None = None
    subscription_id: UUID | None = None
    invoice_id: UUID | None = None
    service_order_id: UUID | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert event to dictionary for JSON serialization."""

        def _serialize(value: Any) -> Any:
            if isinstance(value, UUID):
                return str(value)
            if isinstance(value, datetime):
                return value.isoformat()
            if isinstance(value, dict):
                return {key: _serialize(val) for key, val in value.items()}
            if isinstance(value, (list, tuple)):
                return [_serialize(item) for item in value]
            return value

        return {
            "event_id": str(self.event_id),
            "event_type": self.event_type.value,
            "occurred_at": self.occurred_at.isoformat(),
            "payload": _serialize(self.payload),
            "context": {
                "actor": self.actor,
                "subscriber_id": str(self.subscriber_id)
                if self.subscriber_id
                else None,
                "account_id": str(self.account_id) if self.account_id else None,
                "subscription_id": str(self.subscription_id)
                if self.subscription_id
                else None,
                "invoice_id": str(self.invoice_id) if self.invoice_id else None,
                "service_order_id": str(self.service_order_id)
                if self.service_order_id
                else None,
            },
        }
