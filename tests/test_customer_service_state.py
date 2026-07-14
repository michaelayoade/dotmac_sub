"""Shared outage-aware customer service state."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from app.models.catalog import AccessState, BillingMode, SubscriptionStatus
from app.models.subscriber import SubscriberStatus
from app.models.support import Ticket, TicketStatus
from app.services.customer_service_state import (
    get_customer_service_state,
    resolve_customer_billing_access_state,
)
from app.services.topology.connection_status import Assessment
from tests.test_customer_plan_change_prepaid import _make_offer, _make_subscription


def _subscription(db_session, subscriber, *, name: str = "CSS Plan"):
    offer = _make_offer(
        db_session,
        name=name,
        amount=Decimal("100.00"),
        plan_family="unlimited",
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        offer,
        next_billing_at=datetime.now(UTC) + timedelta(days=3),
        start_at=datetime.now(UTC) - timedelta(days=27),
    )
    db_session.commit()
    db_session.refresh(subscription)
    return subscription


def _assessment(state: str, *, area: bool = False) -> Assessment:
    return Assessment(
        state=state,
        is_area_outage=area,
        area_boundary_id=uuid.uuid4() if area else None,
        verdict="healthy" if state == "connected" else "node_outage",
        medium="fiber",
        headline="h",
        message="m",
        advice=None,
        checked_at=datetime.now(UTC),
    )


def _resolver_objects(
    *,
    subscriber_status=SubscriberStatus.active,
    subscription_status=SubscriptionStatus.active,
    billing_mode=BillingMode.postpaid,
    is_active=True,
    billing_enabled=True,
    captive_redirect_enabled=False,
):
    subscriber_id = uuid.uuid4()
    subscriber = SimpleNamespace(
        id=subscriber_id,
        status=subscriber_status,
        billing_mode=billing_mode,
        is_active=is_active,
        billing_enabled=billing_enabled,
        captive_redirect_enabled=captive_redirect_enabled,
    )
    subscription = SimpleNamespace(
        id=uuid.uuid4(),
        subscriber_id=subscriber_id,
        status=subscription_status,
        billing_mode=billing_mode,
        subscriber=subscriber,
    )
    return subscriber, subscription


def test_billing_access_resolver_active_postpaid_is_invoice_and_radius_eligible():
    _subscriber, subscription = _resolver_objects()

    state = resolve_customer_billing_access_state(subscription)

    assert state.active_customer_service is True
    assert state.counts_for_customer_impact is True
    assert state.billable_account is True
    assert state.postpaid_invoice_eligible is True
    assert state.prepaid_enforcement_eligible is False
    assert state.radius_access_state == AccessState.active
    assert state.radius_mode == "active"
    assert state.radius_allowed is True
    assert state.access_block_reason is None


def test_billing_access_resolver_active_prepaid_uses_prepaid_enforcement_path():
    _subscriber, subscription = _resolver_objects(billing_mode=BillingMode.prepaid)

    state = resolve_customer_billing_access_state(subscription)

    assert state.active_customer_service is True
    assert state.postpaid_invoice_eligible is False
    assert state.prepaid_enforcement_eligible is True
    assert state.billing_block_reason == "prepaid_not_postpaid_invoice_eligible"
    assert state.radius_mode == "active"


def test_billing_access_resolver_parent_hard_block_overrides_active_subscription():
    _subscriber, subscription = _resolver_objects(
        subscriber_status=SubscriberStatus.disabled
    )

    state = resolve_customer_billing_access_state(subscription)

    assert state.active_customer_service is False
    assert state.billable_account is False
    assert state.postpaid_invoice_eligible is False
    assert state.radius_access_state == AccessState.suspended
    assert state.radius_mode == "reject"
    assert state.radius_allowed is False
    assert state.radius_blocked is True
    assert state.access_block_reason == "subscriber_status_disabled"
    assert state.billing_block_reason == "subscriber_status_disabled"


def test_billing_access_resolver_delinquent_is_billable_but_radius_permissive():
    _subscriber, subscription = _resolver_objects(
        subscriber_status=SubscriberStatus.delinquent
    )

    state = resolve_customer_billing_access_state(subscription)

    assert state.active_customer_service is False
    assert state.counts_for_customer_impact is False
    assert state.billable_account is True
    assert state.postpaid_invoice_eligible is True
    assert state.radius_access_state == AccessState.active
    assert state.radius_allowed is True
    assert state.access_block_reason is None


def test_connected_customer_has_no_suppression(db_session, subscriber, monkeypatch):
    subscription = _subscription(db_session, subscriber)
    monkeypatch.setattr(
        "app.services.topology.connection_status.assess",
        lambda session, sub, now=None: _assessment("connected"),
    )

    state = get_customer_service_state(db_session, subscription)

    assert state.billing_state == "active"
    assert state.connection_state == "connected"
    assert state.area_outage is False
    assert state.active_outage_id is None
    assert state.open_infrastructure_ticket_id is None
    assert state.should_suppress_expiry_notice is False
    assert state.should_suppress_suspension_notice is False
    assert state.extension_candidate is False
    assert state.customer_message is None


def test_area_outage_suppresses_and_links_incident(db_session, subscriber, monkeypatch):
    subscription = _subscription(db_session, subscriber)
    monkeypatch.setattr(
        "app.services.topology.connection_status.assess",
        lambda session, sub, now=None: _assessment("outage", area=True),
    )
    incident_id = uuid.uuid4()

    class _Incident:
        id = incident_id

    monkeypatch.setattr(
        "app.services.topology.customer_path.resolve_customer_path",
        lambda session, sub: object(),
    )
    monkeypatch.setattr(
        "app.services.topology.outage.open_incident_for_path",
        lambda session, path: _Incident(),
    )

    state = get_customer_service_state(db_session, subscription)

    assert state.connection_state == "outage"
    assert state.area_outage is True
    assert state.active_outage_id == incident_id
    assert state.should_suppress_expiry_notice is True
    assert state.should_suppress_suspension_notice is True
    assert state.extension_candidate is True
    assert "service issue in your area" in (state.customer_message or "")
    context = state.support_context()
    assert context["billing_reminders_suppressed"] is True
    assert context["active_outage_id"] == str(incident_id)
    assert context["connection_status_presentation"] == {
        "value": "outage",
        "label": "Area outage",
        "tone": "negative",
        "icon": "alert",
    }


def test_inferred_area_outage_suppresses_without_incident(
    db_session, subscriber, monkeypatch
):
    # Classifier-inferred boundary, no OutageIncident row: still suppress.
    subscription = _subscription(db_session, subscriber)
    monkeypatch.setattr(
        "app.services.topology.connection_status.assess",
        lambda session, sub, now=None: _assessment("outage", area=True),
    )
    monkeypatch.setattr(
        "app.services.topology.customer_path.resolve_customer_path",
        lambda session, sub: object(),
    )
    monkeypatch.setattr(
        "app.services.topology.outage.open_incident_for_path",
        lambda session, path: None,
    )

    state = get_customer_service_state(db_session, subscription)

    assert state.area_outage is True
    assert state.active_outage_id is None
    assert state.should_suppress_expiry_notice is True


def test_open_infrastructure_ticket_suppresses_when_connected(
    db_session, subscriber, monkeypatch
):
    subscription = _subscription(db_session, subscriber)
    ticket = Ticket(
        subscriber_id=subscriber.id,
        title="Customer link disconnection",
        description="Infrastructure down, no internet at site",
        status=TicketStatus.open.value,
        ticket_type="Customer Link Disconnection",
    )
    db_session.add(ticket)
    db_session.commit()
    monkeypatch.setattr(
        "app.services.topology.connection_status.assess",
        lambda session, sub, now=None: _assessment("connected"),
    )

    state = get_customer_service_state(db_session, subscription)

    assert state.connection_state == "connected"
    assert state.area_outage is False
    assert state.open_infrastructure_ticket_id == ticket.id
    assert state.should_suppress_expiry_notice is True
    assert state.should_suppress_suspension_notice is True
    assert state.extension_candidate is False
    assert "reported service issue" in (state.customer_message or "")


def test_non_infrastructure_ticket_does_not_suppress(
    db_session, subscriber, monkeypatch
):
    subscription = _subscription(db_session, subscriber)
    db_session.add(
        Ticket(
            subscriber_id=subscriber.id,
            title="Billing question",
            description="Customer wants a receipt",
            status=TicketStatus.open.value,
            ticket_type="Billing",
        )
    )
    db_session.commit()
    monkeypatch.setattr(
        "app.services.topology.connection_status.assess",
        lambda session, sub, now=None: _assessment("connected"),
    )

    state = get_customer_service_state(db_session, subscription)

    assert state.open_infrastructure_ticket_id is None
    assert state.should_suppress_expiry_notice is False
