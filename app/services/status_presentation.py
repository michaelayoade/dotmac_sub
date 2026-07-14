"""Canonical semantic presentation of domain lifecycle statuses.

Lifecycle services own the status values and transitions. This projection owns
their human label, semantic tone, and icon key so web and mobile clients do not
create competing interpretations. Clients still own concrete colors, spacing,
and platform-native rendering for each semantic tone.
"""

from __future__ import annotations

from enum import Enum

from app.models.billing import InvoiceStatus, PaymentStatus
from app.models.catalog import SubscriptionStatus
from app.models.subscriber import SubscriberStatus
from app.models.support import TicketStatus
from app.schemas.status_presentation import (
    StatusIcon,
    StatusPresentation,
    StatusTone,
)
from app.services.field.work_order_status import WorkOrderStatus
from app.services.topology.connection_status import ConnectionHealthState
from app.services.topology.outage import OutageStatus

_ACCOUNT_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    SubscriberStatus.new.value: ("New", StatusTone.info, StatusIcon.clock),
    SubscriberStatus.active.value: ("Active", StatusTone.positive, StatusIcon.check),
    SubscriberStatus.delinquent.value: (
        "Delinquent",
        StatusTone.warning,
        StatusIcon.alert,
    ),
    SubscriberStatus.suspended.value: (
        "Suspended",
        StatusTone.warning,
        StatusIcon.alert,
    ),
    SubscriberStatus.blocked.value: ("Blocked", StatusTone.negative, StatusIcon.x),
    SubscriberStatus.disabled.value: ("Disabled", StatusTone.negative, StatusIcon.x),
    SubscriberStatus.canceled.value: ("Canceled", StatusTone.negative, StatusIcon.x),
    "inactive": ("Inactive", StatusTone.neutral, StatusIcon.minus),
}

_SUBSCRIPTION_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    SubscriptionStatus.pending.value: ("Pending", StatusTone.info, StatusIcon.clock),
    SubscriptionStatus.active.value: (
        "Active",
        StatusTone.positive,
        StatusIcon.check,
    ),
    SubscriptionStatus.blocked.value: (
        "Blocked",
        StatusTone.negative,
        StatusIcon.x,
    ),
    SubscriptionStatus.suspended.value: (
        "Suspended",
        StatusTone.warning,
        StatusIcon.alert,
    ),
    SubscriptionStatus.stopped.value: (
        "Stopped",
        StatusTone.warning,
        StatusIcon.minus,
    ),
    SubscriptionStatus.disabled.value: (
        "Disabled",
        StatusTone.negative,
        StatusIcon.x,
    ),
    SubscriptionStatus.hidden.value: (
        "Hidden",
        StatusTone.neutral,
        StatusIcon.minus,
    ),
    SubscriptionStatus.archived.value: (
        "Archived",
        StatusTone.neutral,
        StatusIcon.archive,
    ),
    SubscriptionStatus.canceled.value: (
        "Canceled",
        StatusTone.negative,
        StatusIcon.x,
    ),
    SubscriptionStatus.expired.value: (
        "Expired",
        StatusTone.negative,
        StatusIcon.x,
    ),
}

_WORK_ORDER_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    WorkOrderStatus.draft.value: ("Draft", StatusTone.neutral, StatusIcon.archive),
    WorkOrderStatus.scheduled.value: (
        "Scheduled",
        StatusTone.info,
        StatusIcon.clock,
    ),
    WorkOrderStatus.dispatched.value: (
        "Dispatched",
        StatusTone.info,
        StatusIcon.info,
    ),
    WorkOrderStatus.in_progress.value: (
        "In progress",
        StatusTone.info,
        StatusIcon.clock,
    ),
    WorkOrderStatus.paused.value: (
        "Paused",
        StatusTone.warning,
        StatusIcon.alert,
    ),
    WorkOrderStatus.completed.value: (
        "Completed",
        StatusTone.positive,
        StatusIcon.check,
    ),
    WorkOrderStatus.canceled.value: (
        "Canceled",
        StatusTone.negative,
        StatusIcon.x,
    ),
    "cancelled": ("Canceled", StatusTone.negative, StatusIcon.x),
}

_TICKET_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    TicketStatus.new.value: ("New", StatusTone.info, StatusIcon.clock),
    TicketStatus.open.value: ("Open", StatusTone.info, StatusIcon.info),
    TicketStatus.pending.value: ("Pending", StatusTone.warning, StatusIcon.clock),
    TicketStatus.waiting_on_customer.value: (
        "Waiting on customer",
        StatusTone.warning,
        StatusIcon.clock,
    ),
    TicketStatus.lastmile_rerun.value: (
        "Last-mile rerun",
        StatusTone.warning,
        StatusIcon.clock,
    ),
    TicketStatus.site_under_construction.value: (
        "Site under construction",
        StatusTone.warning,
        StatusIcon.alert,
    ),
    TicketStatus.on_hold.value: (
        "On hold",
        StatusTone.warning,
        StatusIcon.alert,
    ),
    TicketStatus.pending_confirmation.value: (
        "Pending confirmation",
        StatusTone.info,
        StatusIcon.clock,
    ),
    TicketStatus.resolved.value: (
        "Resolved",
        StatusTone.positive,
        StatusIcon.check,
    ),
    TicketStatus.closed.value: (
        "Closed",
        StatusTone.neutral,
        StatusIcon.archive,
    ),
    TicketStatus.canceled.value: (
        "Canceled",
        StatusTone.negative,
        StatusIcon.x,
    ),
    TicketStatus.merged.value: (
        "Merged",
        StatusTone.neutral,
        StatusIcon.archive,
    ),
}

_INVOICE_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    InvoiceStatus.draft.value: (
        "Draft",
        StatusTone.neutral,
        StatusIcon.archive,
    ),
    InvoiceStatus.issued.value: (
        "Issued",
        StatusTone.info,
        StatusIcon.info,
    ),
    InvoiceStatus.partially_paid.value: (
        "Partially paid",
        StatusTone.warning,
        StatusIcon.clock,
    ),
    InvoiceStatus.paid.value: (
        "Paid",
        StatusTone.positive,
        StatusIcon.check,
    ),
    InvoiceStatus.void.value: (
        "Void",
        StatusTone.neutral,
        StatusIcon.x,
    ),
    InvoiceStatus.overdue.value: (
        "Overdue",
        StatusTone.negative,
        StatusIcon.alert,
    ),
    InvoiceStatus.written_off.value: (
        "Written off",
        StatusTone.negative,
        StatusIcon.archive,
    ),
}

_PAYMENT_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    PaymentStatus.pending.value: (
        "Pending",
        StatusTone.warning,
        StatusIcon.clock,
    ),
    PaymentStatus.succeeded.value: (
        "Succeeded",
        StatusTone.positive,
        StatusIcon.check,
    ),
    PaymentStatus.failed.value: (
        "Failed",
        StatusTone.negative,
        StatusIcon.x,
    ),
    PaymentStatus.refunded.value: (
        "Refunded",
        StatusTone.neutral,
        StatusIcon.archive,
    ),
    PaymentStatus.partially_refunded.value: (
        "Partially refunded",
        StatusTone.warning,
        StatusIcon.clock,
    ),
    PaymentStatus.canceled.value: (
        "Canceled",
        StatusTone.neutral,
        StatusIcon.x,
    ),
}

_OUTAGE_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    OutageStatus.open.value: ("Open", StatusTone.negative, StatusIcon.alert),
    OutageStatus.suspected.value: (
        "Suspected",
        StatusTone.warning,
        StatusIcon.clock,
    ),
    OutageStatus.confirmed.value: (
        "Confirmed",
        StatusTone.negative,
        StatusIcon.alert,
    ),
    OutageStatus.clearing.value: (
        "Clearing",
        StatusTone.info,
        StatusIcon.clock,
    ),
    OutageStatus.resolved.value: (
        "Resolved",
        StatusTone.positive,
        StatusIcon.check,
    ),
    OutageStatus.discarded.value: (
        "Discarded",
        StatusTone.neutral,
        StatusIcon.x,
    ),
}

_DEVICE_OPERATIONAL_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    "up": ("Up", StatusTone.positive, StatusIcon.check),
    "degraded": ("Degraded", StatusTone.warning, StatusIcon.alert),
    "down": ("Down", StatusTone.negative, StatusIcon.x),
    "maintenance": ("Maintenance", StatusTone.neutral, StatusIcon.minus),
}

_CONNECTION_HEALTH_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    ConnectionHealthState.connected.value: (
        "Connected",
        StatusTone.positive,
        StatusIcon.check,
    ),
    ConnectionHealthState.trouble.value: (
        "Connection issue",
        StatusTone.warning,
        StatusIcon.alert,
    ),
    ConnectionHealthState.outage.value: (
        "Area outage",
        StatusTone.negative,
        StatusIcon.alert,
    ),
}

_ACCESS_SESSION_PRESENTATIONS: dict[str, tuple[str, StatusTone, StatusIcon]] = {
    "connected": ("Connected", StatusTone.positive, StatusIcon.check),
    "stale": ("Last seen", StatusTone.warning, StatusIcon.clock),
    "offline": ("Not connected", StatusTone.neutral, StatusIcon.x),
    "inactive": ("Not connected", StatusTone.neutral, StatusIcon.minus),
}


def _status_value(status: object | None) -> str:
    if isinstance(status, Enum):
        return str(status.value).strip().lower()
    return str(status or "").strip().lower()


def _fallback(value: str) -> StatusPresentation:
    normalized = value or "unknown"
    return StatusPresentation(
        value=normalized,
        label=normalized.replace("_", " ").title(),
        tone=StatusTone.neutral,
        icon=StatusIcon.info,
    )


def _presentation(
    value: str,
    presentations: dict[str, tuple[str, StatusTone, StatusIcon]],
) -> StatusPresentation:
    definition = presentations.get(value)
    if definition is None:
        return _fallback(value)
    label, tone, icon = definition
    return StatusPresentation(value=value, label=label, tone=tone, icon=icon)


def account_status_presentation(
    status: SubscriberStatus | str | None,
    *,
    is_active: bool | None = None,
) -> StatusPresentation:
    """Project an account status without deriving or changing lifecycle state."""
    value = _status_value(status)
    if not value and is_active is not None:
        value = "active" if is_active else "inactive"
    return _presentation(value, _ACCOUNT_PRESENTATIONS)


def subscription_status_presentation(
    status: SubscriptionStatus | str | None,
) -> StatusPresentation:
    """Project a subscription status without deriving service availability."""
    return _presentation(_status_value(status), _SUBSCRIPTION_PRESENTATIONS)


def work_order_status_presentation(
    status: WorkOrderStatus | str | None,
) -> StatusPresentation:
    """Project field work-order status without changing workflow state."""
    return _presentation(_status_value(status), _WORK_ORDER_PRESENTATIONS)


def ticket_status_presentation(
    status: TicketStatus | str | None,
) -> StatusPresentation:
    """Project support-ticket status without changing lifecycle state."""
    return _presentation(_status_value(status), _TICKET_PRESENTATIONS)


def invoice_status_presentation(
    status: InvoiceStatus | str | None,
) -> StatusPresentation:
    """Project invoice lifecycle status without deriving collectibility."""
    return _presentation(_status_value(status), _INVOICE_PRESENTATIONS)


def payment_status_presentation(
    status: PaymentStatus | str | None,
) -> StatusPresentation:
    """Project payment lifecycle status without deriving settlement state."""
    return _presentation(_status_value(status), _PAYMENT_PRESENTATIONS)


def outage_status_presentation(
    status: OutageStatus | str | None,
) -> StatusPresentation:
    """Project persisted outage lifecycle status without changing visibility."""
    return _presentation(_status_value(status), _OUTAGE_PRESENTATIONS)


def device_operational_status_presentation(
    status: object | str | None,
    *,
    retry_pending: bool | None = None,
) -> StatusPresentation:
    """Project derived NOC state while preserving retry/alarm semantics.

    A retry-pending ``down`` remains visibly down under the checked-in binary
    operational model, but uses a warning/clock treatment so a monitoring gap
    cannot be mistaken for negative device evidence.
    """
    value = _status_value(getattr(status, "status", status))
    if retry_pending is None:
        retry_pending = bool(getattr(status, "retry_pending", False))
    if value == "down" and retry_pending:
        return StatusPresentation(
            value=value,
            label="Down",
            tone=StatusTone.warning,
            icon=StatusIcon.clock,
        )
    return _presentation(value, _DEVICE_OPERATIONAL_PRESENTATIONS)


def connection_health_status_presentation(
    status: ConnectionHealthState | str | None,
) -> StatusPresentation:
    """Project customer-safe connection health without re-diagnosing it."""
    return _presentation(_status_value(status), _CONNECTION_HEALTH_PRESENTATIONS)


def access_session_status_presentation(status: str | None) -> StatusPresentation:
    """Project the admin RADIUS-session observation without deriving health."""
    return _presentation(_status_value(status), _ACCESS_SESSION_PRESENTATIONS)
