"""Shared, outage-aware customer service state (single source of truth).

Billing sweeps, notification policy, support tooling, and the portal/mobile
connection surfaces all need the same answer to "is this customer currently
affected by an infrastructure fault, and how should we treat them?". Each
module inventing its own outage logic drifts, so this service owns the
predicates; consumers (``app.tasks.catalog`` today; dunning/enforcement,
ticket deflection and support context next) call in rather than re-derive.

Two access patterns, deliberately separate:

- ``get_customer_service_state(session, subscription)`` — the full
  per-customer view. Resolves the customer's network path via
  ``topology.connection_status`` (real graph work), so it is right for portal
  pages, support context and ticket deflection — NOT for sweeps.
- The batch helpers (``subscription_ids_under_active_outage``,
  ``subscribers_with_open_infrastructure_down_tickets``) — set-based, one
  query pass over the whole cohort, right for Celery sweeps over thousands of
  subscriptions (expiry reminders, dunning, enforcement retries).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.models.catalog import AccessState, BillingMode, SubscriptionStatus
from app.models.subscriber import SubscriberStatus
from app.services.billing_settings import COLLECTIBLE_SERVICE_STATUSES
from app.services.billing_statuses import BILLABLE_SUBSCRIBER_STATUSES
from app.services.customer_support_links import (
    ticket_customer_any_link_filter,
    ticket_customer_link_filter,
    ticket_customer_linked_ids,
)
from app.services.radius_access_state import derive_access_state
from app.services.subscriber_access_policy import RADIUS_BLOCKING_SUBSCRIBER_STATUSES

logger = logging.getLogger(__name__)

ACTIVE_CUSTOMER_SUBSCRIBER_STATUSES = frozenset({SubscriberStatus.active})
RADIUS_PERMISSIVE_SUBSCRIBER_STATUSES = frozenset(
    {
        SubscriberStatus.active,
        SubscriberStatus.delinquent,
    }
)


def active_customer_subscription_filters(subscription_model, subscriber_model) -> tuple:
    """SQL predicates for subscriptions that count as active customers.

    This is intentionally stricter than billing eligibility: customer-impact
    metrics should count subscribers with live service, not blocked/delinquent
    accounts that billing may still chase.
    """
    return (
        subscription_model.status == SubscriptionStatus.active,
        subscriber_model.status.in_(ACTIVE_CUSTOMER_SUBSCRIBER_STATUSES),
        subscriber_model.is_active.is_(True),
    )


def postpaid_invoice_eligible_filters(subscription_model, subscriber_model) -> tuple:
    """SQL predicates mirroring the default postpaid invoice-cycle cohort."""
    return (
        subscription_model.status == SubscriptionStatus.active,
        subscriber_model.status.in_(BILLABLE_SUBSCRIBER_STATUSES),
        subscription_model.billing_mode != BillingMode.prepaid,
    )


def prepaid_enforcement_eligible_filters(subscription_model, subscriber_model) -> tuple:
    """SQL predicates for prepaid balance enforcement / exposure cohorts."""
    return (
        subscription_model.status.in_(COLLECTIBLE_SERVICE_STATUSES),
        subscriber_model.status.in_(BILLABLE_SUBSCRIBER_STATUSES),
        subscription_model.billing_mode == BillingMode.prepaid,
    )


OPEN_INFRASTRUCTURE_TICKET_STATUSES = {
    "new",
    "open",
    "pending",
    "waiting_on_customer",
    "lastmile_rerun",
    "site_under_construction",
    "on_hold",
    "pending_confirmation",
}

INFRASTRUCTURE_DOWN_TICKET_MARKERS = (
    "infrastructure down",
    "service down",
    "internet down",
    "no internet",
    "outage",
    "link down",
    "fiber cut",
    "link disconnection",
    "customer link disconnection",
    "multiple customer link disconnection",
    "core link disconnection",
    "multiple core link disconnection",
    "cabinet disconnection",
    "multiple cabinet disconnection",
    "multiple cabinet link disconnection",
    "access point outage",
    "bts outage",
)

# Billing-comms copy when reminders are paused. Kept customer-safe: no node
# names, no incident internals (same discipline as topology.connection_status).
_AREA_OUTAGE_BILLING_MESSAGE = (
    "We're aware of a service issue in your area. Billing reminders are "
    "paused while this outage is active."
)
_TICKET_BILLING_MESSAGE = (
    "We're working on a reported service issue affecting your connection. "
    "Billing reminders are paused while it's open."
)


@dataclass
class CustomerServiceState:
    """Outage-aware treatment decision for one subscription.

    ``active_outage_id`` is set only when an ``OutageIncident`` covers the
    customer's path; an *inferred* area outage (classifier boundary with no
    incident row yet) still sets ``area_outage``/suppression, just without an
    incident to link. ``customer_message`` is the billing-comms softener —
    the connection-status wording itself comes from
    ``topology.connection_status``.
    """

    subscription_id: uuid.UUID
    subscriber_id: uuid.UUID | None
    billing_state: str
    connection_state: str
    area_outage: bool
    active_outage_id: uuid.UUID | None
    open_infrastructure_ticket_id: uuid.UUID | None
    should_suppress_expiry_notice: bool
    should_suppress_suspension_notice: bool
    extension_candidate: bool
    customer_message: str | None
    checked_at: datetime

    def support_context(self) -> dict[str, Any]:
        """Flat dict for support/agent surfaces (subscriber detail page)."""
        return {
            "billing_state": self.billing_state,
            "connection_state": self.connection_state,
            "area_outage": self.area_outage,
            "active_outage_id": (
                str(self.active_outage_id) if self.active_outage_id else None
            ),
            "open_infrastructure_ticket_id": (
                str(self.open_infrastructure_ticket_id)
                if self.open_infrastructure_ticket_id
                else None
            ),
            "billing_reminders_suppressed": self.should_suppress_expiry_notice,
            "extension_candidate": self.extension_candidate,
            "checked_at": self.checked_at.isoformat(),
        }


@dataclass(frozen=True)
class CustomerBillingAccessState:
    """Shared billing/access decision for one subscription.

    The fields deliberately separate customer-impact, billing, prepaid
    enforcement and RADIUS. Those answers use overlapping data, but they are
    not the same rule.
    """

    subscription_id: object | None
    subscriber_id: object | None
    subscriber_status: str | None
    subscription_status: str | None
    account_billing_mode: str | None
    subscription_billing_mode: str | None
    account_enabled: bool
    account_billing_enabled: bool
    active_customer_service: bool
    billable_account: bool
    postpaid_invoice_eligible: bool
    prepaid_enforcement_eligible: bool
    counts_for_customer_impact: bool
    radius_access_state: AccessState | None
    radius_allowed: bool
    radius_blocked: bool
    radius_mode: str
    access_block_reason: str | None
    billing_block_reason: str | None


def resolve_customer_billing_access_state(
    subscription,
    *,
    subscriber=None,
    captive_redirect_enabled: bool | None = None,
) -> CustomerBillingAccessState:
    """Resolve the account/subscription billing and access state.

    Use this for in-memory decisions after a subscription row has already been
    loaded. SQL-heavy jobs should use the companion filter helpers above for
    coarse selection, then this resolver for per-row decisions.
    """
    subscriber = (
        subscriber
        if subscriber is not None
        else getattr(subscription, "subscriber", None)
    )

    subscription_status = getattr(subscription, "status", None)
    subscriber_status = getattr(subscriber, "status", None) if subscriber else None
    subscription_mode = getattr(subscription, "billing_mode", None)
    account_mode = getattr(subscriber, "billing_mode", None) if subscriber else None

    account_enabled = bool(
        subscriber is not None and getattr(subscriber, "is_active", False)
    )
    account_billing_enabled = bool(
        subscriber is not None and getattr(subscriber, "billing_enabled", True)
    )
    billable_account = (
        account_enabled
        and account_billing_enabled
        and subscriber_status in BILLABLE_SUBSCRIBER_STATUSES
    )
    active_customer_service = (
        account_enabled
        and subscriber_status in ACTIVE_CUSTOMER_SUBSCRIBER_STATUSES
        and subscription_status == SubscriptionStatus.active
    )
    postpaid_invoice_eligible = (
        billable_account
        and subscription_status == SubscriptionStatus.active
        and subscription_mode != BillingMode.prepaid
    )
    prepaid_enforcement_eligible = (
        billable_account
        and subscription_status in COLLECTIBLE_SERVICE_STATUSES
        and subscription_mode == BillingMode.prepaid
    )

    captive = (
        bool(captive_redirect_enabled)
        if captive_redirect_enabled is not None
        else bool(getattr(subscriber, "captive_redirect_enabled", False))
    )
    account_hard_reject = _account_radius_hard_reject(
        subscriber_status=subscriber_status,
        account_enabled=account_enabled,
        subscriber_missing=subscriber is None,
    )
    radius_state = (
        derive_access_state(
            subscription_status,
            captive_redirect_enabled=captive,
            hard_reject=account_hard_reject,
        )
        if isinstance(subscription_status, SubscriptionStatus)
        else None
    )
    if account_hard_reject and radius_state == AccessState.active:
        radius_state = AccessState.suspended
    radius_blocked = radius_state in {AccessState.suspended, AccessState.captive}
    radius_allowed = radius_state in {AccessState.active, AccessState.captive}

    access_block_reason = _access_block_reason(
        subscription_status=subscription_status,
        subscriber_status=subscriber_status,
        account_enabled=account_enabled,
        subscriber_missing=subscriber is None,
        radius_state=radius_state,
    )
    billing_block_reason = _billing_block_reason(
        subscription_status=subscription_status,
        subscriber_status=subscriber_status,
        account_enabled=account_enabled,
        account_billing_enabled=account_billing_enabled,
        subscription_mode=subscription_mode,
    )

    return CustomerBillingAccessState(
        subscription_id=getattr(subscription, "id", None),
        subscriber_id=getattr(subscription, "subscriber_id", None)
        or getattr(subscriber, "id", None),
        subscriber_status=_enum_value(subscriber_status),
        subscription_status=_enum_value(subscription_status),
        account_billing_mode=_enum_value(account_mode),
        subscription_billing_mode=_enum_value(subscription_mode),
        account_enabled=account_enabled,
        account_billing_enabled=account_billing_enabled,
        active_customer_service=active_customer_service,
        billable_account=billable_account,
        postpaid_invoice_eligible=postpaid_invoice_eligible,
        prepaid_enforcement_eligible=prepaid_enforcement_eligible,
        counts_for_customer_impact=active_customer_service,
        radius_access_state=radius_state,
        radius_allowed=radius_allowed,
        radius_blocked=radius_blocked,
        radius_mode=_radius_mode(radius_state),
        access_block_reason=access_block_reason,
        billing_block_reason=billing_block_reason,
    )


def _account_radius_hard_reject(
    *,
    subscriber_status: SubscriberStatus | None,
    account_enabled: bool,
    subscriber_missing: bool,
) -> bool:
    if subscriber_missing or not account_enabled:
        return True
    if subscriber_status in RADIUS_BLOCKING_SUBSCRIBER_STATUSES:
        return True
    if subscriber_status in RADIUS_PERMISSIVE_SUBSCRIBER_STATUSES:
        return False
    return True


def _access_block_reason(
    *,
    subscription_status,
    subscriber_status,
    account_enabled: bool,
    subscriber_missing: bool,
    radius_state: AccessState | None,
) -> str | None:
    if subscriber_missing:
        return "subscriber_missing"
    if not account_enabled:
        return "subscriber_inactive"
    if subscriber_status in RADIUS_BLOCKING_SUBSCRIBER_STATUSES:
        return f"subscriber_status_{_enum_value(subscriber_status)}"
    if radius_state in {
        AccessState.suspended,
        AccessState.captive,
        AccessState.terminated,
    }:
        return f"subscription_status_{_enum_value(subscription_status)}"
    if radius_state is None:
        return f"subscription_unprovisioned_{_enum_value(subscription_status)}"
    return None


def _billing_block_reason(
    *,
    subscription_status,
    subscriber_status,
    account_enabled: bool,
    account_billing_enabled: bool,
    subscription_mode,
) -> str | None:
    if not account_enabled:
        return "subscriber_inactive"
    if not account_billing_enabled:
        return "account_billing_disabled"
    if subscriber_status not in BILLABLE_SUBSCRIBER_STATUSES:
        return f"subscriber_status_{_enum_value(subscriber_status)}"
    if subscription_status not in COLLECTIBLE_SERVICE_STATUSES:
        return f"subscription_status_{_enum_value(subscription_status)}"
    if subscription_mode == BillingMode.prepaid:
        return "prepaid_not_postpaid_invoice_eligible"
    return None


def _enum_value(value) -> str | None:
    if value is None:
        return None
    return getattr(value, "value", str(value))


def _radius_mode(state: AccessState | None) -> str:
    if state is None:
        return "none"
    if state == AccessState.suspended:
        return "reject"
    return state.value


def get_customer_service_state(
    session: Session, subscription, *, now: datetime | None = None
) -> CustomerServiceState:
    """Assess one subscription: connection state + fault-aware billing flags."""
    from app.services.topology.connection_status import assess

    a = assess(session, subscription, now=now)

    active_outage_id: uuid.UUID | None = None
    if a.is_area_outage:
        active_outage_id = _incident_id_for_subscription(session, subscription)

    ticket = open_infrastructure_down_ticket(session, subscription.subscriber_id)
    suppress = a.is_area_outage or ticket is not None

    if a.is_area_outage:
        customer_message = _AREA_OUTAGE_BILLING_MESSAGE
    elif ticket is not None:
        customer_message = _TICKET_BILLING_MESSAGE
    else:
        customer_message = None

    status = getattr(subscription, "status", None)
    return CustomerServiceState(
        subscription_id=subscription.id,
        subscriber_id=subscription.subscriber_id,
        billing_state=getattr(status, "value", str(status or "")),
        connection_state=a.state,
        area_outage=a.is_area_outage,
        active_outage_id=active_outage_id,
        open_infrastructure_ticket_id=ticket.id if ticket is not None else None,
        should_suppress_expiry_notice=suppress,
        should_suppress_suspension_notice=suppress,
        extension_candidate=a.is_area_outage,
        customer_message=customer_message,
        checked_at=a.checked_at,
    )


def _incident_id_for_subscription(session: Session, subscription) -> uuid.UUID | None:
    """The live OutageIncident covering this customer's path, if any."""
    from app.services.topology.customer_path import resolve_customer_path
    from app.services.topology.outage import open_incident_for_path

    try:
        path = resolve_customer_path(session, subscription)
        incident = open_incident_for_path(session, path)
    except Exception:  # advisory link only; the area_outage flag already stands
        logger.exception(
            "Failed to resolve outage incident for subscription %s", subscription.id
        )
        return None
    return incident.id if incident is not None else None


# ---------------------------------------------------------------------------
# Batch helpers (Celery sweeps) — set-based, no per-customer path resolution.
# ---------------------------------------------------------------------------


def open_infrastructure_down_ticket(session: Session, subscriber_id):
    """The subscriber's open infrastructure-down ticket, or None."""
    if not subscriber_id:
        return None
    from app.models.support import Ticket

    tickets = (
        session.query(Ticket)
        .filter(Ticket.is_active.is_(True))
        .filter(Ticket.status.in_(OPEN_INFRASTRUCTURE_TICKET_STATUSES))
        .filter(ticket_customer_link_filter(Ticket, subscriber_id))
        .all()
    )
    for ticket in tickets:
        if is_infrastructure_down_ticket(ticket):
            return ticket
    return None


def subscribers_with_open_infrastructure_down_tickets(
    session: Session,
    subscriber_ids: set[object],
) -> set[object]:
    """Subset of ``subscriber_ids`` holding an open infrastructure-down ticket."""
    from app.models.support import Ticket

    subscriber_ids = {
        subscriber_id for subscriber_id in subscriber_ids if subscriber_id
    }
    if not subscriber_ids:
        return set()

    tickets = (
        session.query(Ticket)
        .filter(Ticket.is_active.is_(True))
        .filter(Ticket.status.in_(OPEN_INFRASTRUCTURE_TICKET_STATUSES))
        .filter(ticket_customer_any_link_filter(Ticket, subscriber_ids))
        .all()
    )
    suppressed: set[object] = set()
    for ticket in tickets:
        if not is_infrastructure_down_ticket(ticket):
            continue
        for ticket_subscriber_id in ticket_customer_linked_ids(ticket):
            if ticket_subscriber_id in subscriber_ids:
                suppressed.add(ticket_subscriber_id)
    return suppressed


def subscription_ids_under_active_outage(
    session: Session, subscriptions
) -> set[object]:
    """Subset of ``subscriptions`` inside a live outage incident's blast radius."""
    subscription_ids = {sub.id for sub in subscriptions if sub.id}
    if not subscription_ids:
        return set()
    return active_outage_subscription_ids(session) & subscription_ids


def active_outage_subscription_ids(session: Session) -> set[object]:
    """Every subscription id inside the blast radius of a live outage incident.

    Which incidents count (customer-impact policy):

    - manual operator ``open``: until resolved — a human declared it and a
      human owns closing it;
    - classifier ``confirmed``/``clearing``: always — that lifecycle debounces
      and auto-resolves itself;
    - auto-detect operator ``open`` (``declared_by = system:outage-autodetect``):
      only while FRESH. The legacy auto-detect path opens incidents that
      nothing auto-resolves, and a few days of accumulated zombies once put
      97% of the fleet "under outage" and suppressed nearly every billing
      notice. Freshness window: ``autodetect_incident_impact_ttl_hours``
      (default 6).

    The blast radius per incident scope (root node / basestation / FDH
    cabinet) comes from ``topology.affected``.
    """
    from app.models.domain_settings import SettingDomain
    from app.models.network_monitoring import NetworkDevice, OutageIncident, PopSite
    from app.services.settings_spec import resolve_value
    from app.services.topology.outage import (
        AUTO_DETECT_ACTOR,
        CLASSIFIER_CUSTOMER_VISIBLE_STATUSES,
    )

    live_statuses = ("open", *CLASSIFIER_CUSTOMER_VISIBLE_STATUSES)
    incidents = (
        session.query(OutageIncident)
        .filter(OutageIncident.status.in_(live_statuses))
        .all()
    )
    if not incidents:
        return set()

    try:
        ttl_hours = int(
            resolve_value(
                session,
                SettingDomain.network_monitoring,
                "autodetect_incident_impact_ttl_hours",
            )
            or 6
        )
    except (TypeError, ValueError):
        ttl_hours = 6
    autodetect_cutoff = datetime.now(UTC) - timedelta(hours=max(1, ttl_hours))

    def _counts_for_impact(incident) -> bool:
        if incident.status != "open" or incident.declared_by != AUTO_DETECT_ACTOR:
            return True
        started = incident.started_at
        if started is None:
            return False
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        return started >= autodetect_cutoff

    incidents = [incident for incident in incidents if _counts_for_impact(incident)]
    if not incidents:
        return set()

    from app.models.network import FdhCabinet
    from app.services.topology.affected import affected_customers

    affected: set[object] = set()
    for incident in incidents:
        node = (
            session.get(NetworkDevice, incident.root_node_id)
            if incident.root_node_id
            else None
        )
        basestation = (
            session.get(PopSite, incident.basestation_id)
            if incident.basestation_id
            else None
        )
        fdh = (
            session.get(FdhCabinet, incident.fdh_cabinet_id)
            if incident.fdh_cabinet_id
            else None
        )
        if node is None and basestation is None and fdh is None:
            continue
        try:
            impact = affected_customers(
                session, node=node, basestation=basestation, fdh=fdh
            )
        except Exception:
            # One unresolvable incident must not block the whole sweep; the
            # ticket-based suppression still covers reported faults.
            logger.exception(
                "Failed to resolve outage blast radius for incident %s", incident.id
            )
            continue
        affected.update(
            s.id for s in impact.get("subscriptions", []) if getattr(s, "id", None)
        )
    return affected


def is_infrastructure_down_ticket(ticket: Any) -> bool:
    """Free-text match: does this ticket describe an infrastructure fault?

    ``ticket_type`` is a free-form String (no enum), so markers are matched
    over type/title/description/tags/metadata — same contract the expiry
    suppression has used since it shipped.
    """
    parts: list[str] = [
        str(getattr(ticket, "ticket_type", "") or ""),
        str(getattr(ticket, "title", "") or ""),
        str(getattr(ticket, "description", "") or ""),
    ]
    tags = getattr(ticket, "tags", None)
    if isinstance(tags, list):
        parts.extend(str(tag or "") for tag in tags)
    metadata = getattr(ticket, "metadata_", None)
    if isinstance(metadata, dict):
        for key in ("ticket_type", "category", "issue", "reason", "source"):
            parts.append(str(metadata.get(key) or ""))
    text = " ".join(parts).strip().lower()
    text = " ".join(text.split())
    return any(marker in text for marker in INFRASTRUCTURE_DOWN_TICKET_MARKERS)
