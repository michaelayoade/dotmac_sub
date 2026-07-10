"""Shared outage-aware customer service state."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.support import Ticket, TicketStatus
from app.services.customer_service_state import get_customer_service_state
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
