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
    subscriber_unthrottled = "subscriber.unthrottled"

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

    # Billing - Payment events (4)
    payment_received = "payment.received"
    payment_failed = "payment.failed"
    payment_refunded = "payment.refunded"
    payment_reversed = "payment.reversed"
    account_credit_deposited = "account_credit.deposited"

    # Billing - Consolidated billing account payment (1)
    billing_account_payment_received = "billing_account.payment_received"

    # Billing - Payment arrangement events (1)
    arrangement_defaulted = "arrangement.defaulted"

    # Billing - Outage compensation (1)
    service_extended = "billing.service_extended"

    # Usage events (5)
    usage_recorded = "usage.recorded"
    usage_warning = "usage.warning"
    usage_exhausted = "usage.exhausted"
    usage_topped_up = "usage.topped_up"
    addon_expiring = "usage.addon_expiring"

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

    # Native sales vertical events. Future agent/inbox
    # lead/quote lifecycle was webhook-silent in the CRM's event system;
    # automation consumes these.
    lead_created = "lead.created"
    quote_accepted = "quote.accepted"
    sales_order_paid = "sales_order.paid"

    # Network events (5)
    device_offline = "device.offline"
    device_online = "device.online"
    device_projection_reconciled = "device_projection.reconciled"
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
    ont_config_updated = "ont.config_updated"
    ont_moved = "ont.moved"
    ont_feature_toggled = "ont.feature_toggled"
    ont_ddm_alert = "ont.ddm_alert"

    # ONT destructive operations (audit events)
    ont_authorized = "ont.authorized"
    ont_deauthorized = "ont.deauthorized"
    ont_factory_reset = "ont.factory_reset"
    ont_rebooted = "ont.rebooted"
    ont_service_port_created = "ont.service_port_created"
    ont_service_port_deleted = "ont.service_port_deleted"
    ont_tr069_bound = "ont.tr069_bound"

    # ONT credential changes (audit events)
    ont_pppoe_credentials_set = "ont.pppoe_credentials_set"
    ont_wifi_password_set = "ont.wifi_password_set"
    ont_wifi_config_updated = "ont.wifi_config_updated"

    # ONT lifecycle events
    ont_decommissioned = "ont.decommissioned"

    # OLT circuit breaker events
    olt_circuit_opened = "olt.circuit_opened"
    olt_circuit_closed = "olt.circuit_closed"

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
    customer_password_changed = "customer.password_changed"  # noqa: S105

    # Reseller events (5)
    reseller_created = "reseller.created"
    reseller_user_provisioned = "reseller_user.provisioned"
    reseller_login = "reseller.login"
    reseller_logout = "reseller.logout"
    reseller_impersonated = "reseller.impersonated"

    # Staff and subscriber identity/authorization lifecycle (6)
    staff_account_provisioned = "staff_account.provisioned"
    staff_account_roles_changed = "staff_account.roles_changed"
    staff_account_activated = "staff_account.activated"
    staff_account_deactivated = "staff_account.deactivated"
    system_user_assignments_changed = "system_user.assignments_changed"
    subscriber_assignments_changed = "subscriber.assignments_changed"

    # Credential recovery lifecycle (2)
    password_recovery_requested = "password_recovery.requested"
    password_recovery_completed = "password_recovery.completed"

    # Referral-created customer credential enrollment lifecycle (2)
    customer_credential_enrollment_requested = (
        "customer_credential_enrollment.requested"
    )
    customer_credential_enrollment_completed = (
        "customer_credential_enrollment.completed"
    )

    # Referral program lifecycle (7) and account conversion lifecycle (1)
    referral_code_issued = "referral_code.issued"
    referral_captured = "referral.captured"
    referral_subscriber_attached = "referral.subscriber_attached"
    referral_qualified = "referral.qualified"
    referral_expired = "referral.expired"
    referral_rejected = "referral.rejected"
    referral_reward_issued = "referral.reward_issued"
    referral_reward_reconciled = "referral.reward_reconciled"
    referral_account_converted = "referral_account.converted"

    # Account-adjustment financial evidence lifecycle (2)
    account_adjustment_confirmed = "account_adjustment.confirmed"
    account_adjustment_reversed = "account_adjustment.reversed"

    # RBAC catalog events (2)
    rbac_role_catalog_changed = "rbac.role_catalog_changed"
    rbac_permission_catalog_changed = "rbac.permission_catalog_changed"

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

    # Outage classifier customer notifications (design docs/designs/OUTAGE_CLASSIFIER.md
    # §P4). Emitted by the outage notifier so the notification system owns channel
    # selection + per-subscriber preferences; the notifier only supplies content.
    outage_area = "outage.area"
    outage_last_mile = "outage.last_mile"

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
    # Expiry is a distinct terminal transition. It is recorded as ``other``
    # (with reason="expired" in the payload) rather than a dedicated
    # ``expire`` LifecycleEventType: that enum is a native Postgres type and
    # adding a value needs an ALTER TYPE migration, deferred until the alembic
    # heads are merged. Mapping it here closes the audit hole where expiry
    # produced no lifecycle record at all.
    EventType.subscription_expired: "other",
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
