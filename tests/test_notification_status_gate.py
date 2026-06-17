"""Tests for the account-status notification gate."""

from __future__ import annotations

import pytest

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.notification import Notification
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services.events.handlers.notification import NotificationHandler
from app.services.events.types import Event, EventType
from app.services.notification_status_policy import status_allows_notification


@pytest.mark.parametrize(
    "status,category,expected",
    [
        # Terminal: nothing, ever.
        (SubscriberStatus.canceled, "billing", False),
        (SubscriberStatus.canceled, "account", False),
        (SubscriberStatus.disabled, "billing", False),
        (SubscriberStatus.disabled, "service", False),
        # Walled: actionable categories only.
        (SubscriberStatus.suspended, "billing", True),
        (SubscriberStatus.suspended, "account", True),
        (SubscriberStatus.suspended, "service", True),
        (SubscriberStatus.suspended, "usage", False),
        (SubscriberStatus.blocked, "billing", True),
        (SubscriberStatus.blocked, "usage", False),
        # Unrestricted.
        (SubscriberStatus.active, "usage", True),
        (SubscriberStatus.new, "usage", True),
        (SubscriberStatus.delinquent, "usage", True),
        # Unknown subscriber → never silently dropped.
        (None, "billing", True),
    ],
)
def test_status_policy(status, category, expected):
    assert status_allows_notification(status, category) is expected


def _subscriber(db, status, suffix: str) -> Subscriber:
    sub = Subscriber(
        first_name="Gate",
        last_name=suffix,
        email=f"gate-{suffix.lower()}@example.com",
        status=status,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def _handle(db, event_type, account_id):
    NotificationHandler().handle(
        db,
        Event(
            event_type=event_type,
            payload={"invoice_id": "00000000-0000-0000-0000-000000000001"},
            account_id=account_id,
        ),
    )
    # The handler db.add()s notifications but doesn't flush; the test session has
    # autoflush off, so flush to make queued rows visible to the count queries.
    db.flush()


def test_canceled_account_gets_no_billing_notification(db_session):
    sub = _subscriber(db_session, SubscriberStatus.canceled, "Cancel")
    before = db_session.query(Notification).count()
    _handle(db_session, EventType.invoice_overdue, sub.id)  # billing category
    assert db_session.query(Notification).count() == before


def test_active_account_gets_billing_notification(db_session):
    sub = _subscriber(db_session, SubscriberStatus.active, "Active")
    before = db_session.query(Notification).count()
    _handle(db_session, EventType.invoice_overdue, sub.id)
    assert db_session.query(Notification).count() > before


def test_suspended_account_billing_allowed_usage_suppressed(db_session):
    sub = _subscriber(db_session, SubscriberStatus.suspended, "Susp")
    base = db_session.query(Notification).count()
    _handle(db_session, EventType.usage_warning, sub.id)  # usage → suppressed
    assert db_session.query(Notification).count() == base
    _handle(db_session, EventType.invoice_overdue, sub.id)  # billing → allowed
    assert db_session.query(Notification).count() > base


def test_kill_switch_disables_gate(db_session):
    db_session.add(
        DomainSetting(
            domain=SettingDomain.notification,
            key="status_gate_enabled",
            value_text="false",
        )
    )
    db_session.commit()
    sub = _subscriber(db_session, SubscriberStatus.canceled, "KillSwitch")
    before = db_session.query(Notification).count()
    _handle(db_session, EventType.invoice_overdue, sub.id)
    # Gate disabled → the canceled account is no longer blocked by status.
    assert db_session.query(Notification).count() > before
