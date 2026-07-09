"""Subscription expiry reminder suppression rules."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.catalog import SubscriptionStatus
from app.models.support import Ticket, TicketStatus
from app.services.events.types import EventType
from app.tasks import catalog as catalog_tasks
from tests.test_customer_plan_change_prepaid import _make_offer, _make_subscription


@contextmanager
def _use_test_session(db_session):
    yield db_session


def _expiring_subscription(db_session, subscriber):
    offer = _make_offer(
        db_session,
        name="Unlimited Expiry Reminder",
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
        "total_expiring": 1,
    }
    assert len(events) == 1
    args, kwargs = events[0]
    assert args[1] == EventType.subscription_expiring
    assert kwargs["subscription_id"] == subscription.id
    assert kwargs["account_id"] == subscriber.id


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
