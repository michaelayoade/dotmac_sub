"""Subscription expiry reminder suppression rules."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from app.models.catalog import SubscriptionStatus
from app.models.network_monitoring import NetworkDevice, OutageIncident
from app.models.subscriber import Subscriber
from app.models.support import Ticket, TicketStatus
from app.services.events.types import EventType
from app.tasks import catalog as catalog_tasks
from tests.test_customer_plan_change_prepaid import _make_offer, _make_subscription


@contextmanager
def _use_test_session(db_session):
    yield db_session


def _expiring_subscription(
    db_session,
    subscriber,
    *,
    name: str = "Unlimited Expiry Reminder",
):
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
    subscription.end_at = datetime.now(UTC) + timedelta(days=3)
    db_session.commit()
    db_session.refresh(subscription)
    return subscription


def _make_subscriber(db_session, *, first_name: str = "Second"):
    subscriber = Subscriber(
        first_name=first_name,
        last_name="User",
        email=f"expiry-{uuid4().hex}@example.com",
    )
    db_session.add(subscriber)
    db_session.commit()
    db_session.refresh(subscriber)
    return subscriber


def _run_with_test_session(monkeypatch, db_session):
    monkeypatch.setattr(catalog_tasks, "billing_enabled", lambda session: True)
    monkeypatch.setattr(
        catalog_tasks.db_session_adapter,
        "session",
        lambda: _use_test_session(db_session),
    )


def test_send_expiry_reminders_suppresses_open_infrastructure_down_ticket(
    db_session, subscriber, monkeypatch
):
    subscription = _expiring_subscription(db_session, subscriber)
    db_session.add(
        Ticket(
            subscriber_id=subscriber.id,
            title="Customer link disconnection",
            description="Infrastructure down, no internet at site",
            status=TicketStatus.open.value,
            ticket_type="Customer Link Disconnection",
        )
    )
    db_session.commit()
    _run_with_test_session(monkeypatch, db_session)
    events = []
    monkeypatch.setattr(
        "app.services.events.emit_event",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )

    result = catalog_tasks.send_expiry_reminders(days_before=7)

    assert result == {
        "reminded": 0,
        "suppressed_infrastructure_down": 1,
        "suppressed_active_outage": 0,
        "total_expiring": 1,
    }
    assert events == []
    db_session.refresh(subscription)
    assert subscription.status == SubscriptionStatus.active


def test_send_expiry_reminders_ignores_non_infrastructure_tickets(
    db_session, subscriber, monkeypatch
):
    subscription = _expiring_subscription(db_session, subscriber)
    db_session.add(
        Ticket(
            subscriber_id=subscriber.id,
            title="Billing question",
            description="Customer wants receipt for renewal payment",
            status=TicketStatus.open.value,
            ticket_type="Billing",
        )
    )
    db_session.commit()
    _run_with_test_session(monkeypatch, db_session)
    events = []
    monkeypatch.setattr(
        "app.services.events.emit_event",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )

    result = catalog_tasks.send_expiry_reminders(days_before=7)

    assert result == {
        "reminded": 1,
        "suppressed_infrastructure_down": 0,
        "suppressed_active_outage": 0,
        "total_expiring": 1,
    }
    assert len(events) == 1
    args, kwargs = events[0]
    assert args[1] == EventType.subscription_expiring
    assert kwargs["subscription_id"] == subscription.id
    assert kwargs["account_id"] == subscriber.id


def test_send_expiry_reminders_batches_infrastructure_ticket_lookup(
    db_session, subscriber, monkeypatch
):
    suppressed_subscription = _expiring_subscription(
        db_session,
        subscriber,
        name="Unlimited Expiry Reminder Suppressed",
    )
    other_subscriber = _make_subscriber(db_session)
    reminded_subscription = _expiring_subscription(
        db_session,
        other_subscriber,
        name="Unlimited Expiry Reminder Sent",
    )
    _run_with_test_session(monkeypatch, db_session)
    calls = []

    def _capture_suppressed(_session, subscriber_ids):
        calls.append(set(subscriber_ids))
        return {subscriber.id}

    monkeypatch.setattr(
        catalog_tasks,
        "_subscribers_with_open_infrastructure_down_tickets",
        _capture_suppressed,
    )
    events = []
    monkeypatch.setattr(
        "app.services.events.emit_event",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )

    result = catalog_tasks.send_expiry_reminders(days_before=7)

    assert result == {
        "reminded": 1,
        "suppressed_infrastructure_down": 1,
        "suppressed_active_outage": 0,
        "total_expiring": 2,
    }
    assert calls == [{subscriber.id, other_subscriber.id}]
    assert len(events) == 1
    _, kwargs = events[0]
    assert kwargs["subscription_id"] == reminded_subscription.id
    assert kwargs["account_id"] == other_subscriber.id
    assert suppressed_subscription.id != reminded_subscription.id


def test_send_expiry_reminders_ignores_closed_infrastructure_down_tickets(
    db_session, subscriber, monkeypatch
):
    subscription = _expiring_subscription(db_session, subscriber)
    db_session.add(
        Ticket(
            subscriber_id=subscriber.id,
            title="Infrastructure down",
            description="No internet",
            status=TicketStatus.closed.value,
            ticket_type="Customer Link Disconnection",
        )
    )
    db_session.commit()
    _run_with_test_session(monkeypatch, db_session)
    events = []
    monkeypatch.setattr(
        "app.services.events.emit_event",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )

    result = catalog_tasks.send_expiry_reminders(days_before=7)

    assert result["reminded"] == 1
    assert result["suppressed_infrastructure_down"] == 0
    assert len(events) == 1


def _outage_incident(db_session, *, status: str = "open"):
    node = NetworkDevice(
        name=f"outage-root-{uuid4().hex[:8]}",
        source="zabbix_reconcile",
        is_active=True,
    )
    db_session.add(node)
    db_session.flush()
    incident = OutageIncident(
        root_node_id=node.id,
        status=status,
        detection_source="operator",
        declared_by="test",
    )
    db_session.add(incident)
    db_session.commit()
    return incident


def test_send_expiry_reminders_suppresses_active_outage(
    db_session, subscriber, monkeypatch
):
    subscription = _expiring_subscription(db_session, subscriber)
    _outage_incident(db_session, status="open")
    _run_with_test_session(monkeypatch, db_session)
    monkeypatch.setattr(
        "app.services.topology.affected.affected_customers",
        lambda session, node=None, basestation=None, fdh=None: {
            "subscriptions": [subscription],
        },
    )
    events = []
    monkeypatch.setattr(
        "app.services.events.emit_event",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )

    result = catalog_tasks.send_expiry_reminders(days_before=7)

    assert result == {
        "reminded": 0,
        "suppressed_infrastructure_down": 0,
        "suppressed_active_outage": 1,
        "total_expiring": 1,
    }
    assert events == []
    db_session.refresh(subscription)
    assert subscription.status == SubscriptionStatus.active


def test_send_expiry_reminders_sends_when_outage_resolved(
    db_session, subscriber, monkeypatch
):
    subscription = _expiring_subscription(db_session, subscriber)
    _outage_incident(db_session, status="resolved")
    _run_with_test_session(monkeypatch, db_session)

    def _unexpected(*args, **kwargs):
        raise AssertionError(
            "affected_customers must not run when no live incident exists"
        )

    monkeypatch.setattr(
        "app.services.topology.affected.affected_customers", _unexpected
    )
    events = []
    monkeypatch.setattr(
        "app.services.events.emit_event",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )

    result = catalog_tasks.send_expiry_reminders(days_before=7)

    assert result == {
        "reminded": 1,
        "suppressed_infrastructure_down": 0,
        "suppressed_active_outage": 0,
        "total_expiring": 1,
    }
    assert len(events) == 1
    _, kwargs = events[0]
    assert kwargs["subscription_id"] == subscription.id


def test_send_expiry_reminders_outage_only_suppresses_covered_subscriptions(
    db_session, subscriber, monkeypatch
):
    covered = _expiring_subscription(db_session, subscriber)
    other_subscriber = _make_subscriber(db_session)
    uncovered = _expiring_subscription(
        db_session,
        other_subscriber,
        name="Unlimited Expiry Reminder Uncovered",
    )
    _outage_incident(db_session, status="confirmed")
    _run_with_test_session(monkeypatch, db_session)
    monkeypatch.setattr(
        "app.services.topology.affected.affected_customers",
        lambda session, node=None, basestation=None, fdh=None: {
            "subscriptions": [covered],
        },
    )
    events = []
    monkeypatch.setattr(
        "app.services.events.emit_event",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )

    result = catalog_tasks.send_expiry_reminders(days_before=7)

    assert result == {
        "reminded": 1,
        "suppressed_infrastructure_down": 0,
        "suppressed_active_outage": 1,
        "total_expiring": 2,
    }
    assert len(events) == 1
    _, kwargs = events[0]
    assert kwargs["subscription_id"] == uncovered.id
