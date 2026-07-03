from __future__ import annotations

import pytest

from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationTemplate,
)
from app.models.subscriber import Subscriber, SubscriberStatus
from app.models.support import Ticket
from app.services.events.handlers.notification import NotificationHandler
from app.services.events.types import Event, EventType
from app.services.notification_template_conditions import (
    NotificationTemplateConditionError,
    conditions_match,
    validate_conditions,
)


def _subscriber(db_session, *, suffix: str = "Condition") -> Subscriber:
    subscriber = Subscriber(
        first_name="Template",
        last_name=suffix,
        email=f"template-{suffix.lower()}@example.com",
        phone="+2348012345678",
        status=SubscriberStatus.active,
    )
    db_session.add(subscriber)
    db_session.flush()
    return subscriber


def _invoice_overdue_template(db_session, *, conditions: dict | None = None) -> None:
    db_session.add(
        NotificationTemplate(
            name="Invoice Overdue",
            code="invoice_overdue",
            channel=NotificationChannel.email,
            subject="Invoice overdue",
            body="Invoice overdue notice",
            conditions=conditions or {},
            is_active=True,
        )
    )
    db_session.commit()


def _handle_invoice_overdue(db_session, subscriber: Subscriber) -> None:
    NotificationHandler().handle(
        db_session,
        Event(
            event_type=EventType.invoice_overdue,
            payload={},
            account_id=subscriber.id,
        ),
    )
    db_session.flush()


def test_empty_conditions_match_without_queries(db_session):
    assert conditions_match(db_session, subscriber_id=None, conditions={}) is True


def test_customer_has_open_ticket_condition(db_session):
    subscriber = _subscriber(db_session)
    conditions = {
        "all": [
            {
                "field": "customer_has_open_ticket",
                "operator": "=",
                "value": True,
            }
        ]
    }

    assert (
        conditions_match(db_session, subscriber_id=subscriber.id, conditions=conditions)
        is False
    )

    db_session.add(
        Ticket(
            subscriber_id=subscriber.id,
            title="Slow internet",
            status="open",
            priority="normal",
        )
    )
    db_session.flush()

    assert (
        conditions_match(db_session, subscriber_id=subscriber.id, conditions=conditions)
        is True
    )


def test_invalid_condition_field_is_rejected():
    with pytest.raises(NotificationTemplateConditionError):
        validate_conditions(
            {
                "all": [
                    {
                        "field": "made_up_field",
                        "operator": "=",
                        "value": True,
                    }
                ]
            }
        )


def test_event_notification_suppressed_until_condition_matches(db_session):
    subscriber = _subscriber(db_session, suffix="Gate")
    _invoice_overdue_template(
        db_session,
        conditions={
            "all": [
                {
                    "field": "customer_has_open_ticket",
                    "operator": "=",
                    "value": True,
                }
            ]
        },
    )

    before = db_session.query(Notification).count()
    _handle_invoice_overdue(db_session, subscriber)
    assert db_session.query(Notification).count() == before

    db_session.add(
        Ticket(
            subscriber_id=subscriber.id,
            title="Billing dispute",
            status="open",
            priority="normal",
        )
    )
    db_session.flush()

    _handle_invoice_overdue(db_session, subscriber)
    assert db_session.query(Notification).count() == before + 1
