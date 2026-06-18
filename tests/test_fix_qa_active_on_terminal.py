"""Regression: a terminal subscriber must not carry an active subscription in the
launch-audit population, and the QA cleanup only ever touches QA/test logins.
"""

from __future__ import annotations

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.subscriber import Subscriber, SubscriberStatus
from scripts.one_off.fix_qa_active_on_terminal_subscriber import (
    find_qa_active_on_terminal,
)


def _sub(db, offer, *, login, sub_status, subscriber_status):
    s = Subscriber(
        first_name="Q",
        last_name="A",
        email=f"{id(object())}@e.com",
        status=subscriber_status,
    )
    db.add(s)
    db.flush()
    sub = Subscription(
        subscriber_id=s.id,
        offer_id=offer.id,
        status=sub_status,
        login=login,
    )
    db.add(sub)
    db.flush()
    return s, sub


def test_finds_qa_active_on_canceled_subscriber(db_session, catalog_offer):
    _, sub = _sub(
        db_session,
        catalog_offer,
        login="qa-test-abc",
        sub_status=SubscriptionStatus.active,
        subscriber_status=SubscriberStatus.canceled,
    )
    db_session.commit()
    found = find_qa_active_on_terminal(db_session)
    assert [s.id for s in found] == [sub.id]


def test_skips_qa_on_active_subscriber(db_session, catalog_offer):
    _sub(
        db_session,
        catalog_offer,
        login="qa-test-xyz",
        sub_status=SubscriptionStatus.active,
        subscriber_status=SubscriberStatus.active,  # subscriber NOT terminal
    )
    db_session.commit()
    assert find_qa_active_on_terminal(db_session) == []


def test_never_touches_real_customer_on_terminal_subscriber(db_session, catalog_offer):
    # a real (non-QA) active sub on a canceled subscriber must NOT be auto-canceled
    _sub(
        db_session,
        catalog_offer,
        login="100012345",
        sub_status=SubscriptionStatus.active,
        subscriber_status=SubscriberStatus.canceled,
    )
    db_session.commit()
    assert find_qa_active_on_terminal(db_session) == []


def test_cancel_removes_from_active_population(db_session, catalog_offer):
    """Invariant: after the cleanup, the QA sub is no longer active."""
    _, sub = _sub(
        db_session,
        catalog_offer,
        login="qa-test-def",
        sub_status=SubscriptionStatus.active,
        subscriber_status=SubscriberStatus.canceled,
    )
    db_session.commit()
    for s in find_qa_active_on_terminal(db_session):
        s.status = SubscriptionStatus.canceled
    db_session.commit()
    db_session.refresh(sub)
    assert sub.status == SubscriptionStatus.canceled
    assert find_qa_active_on_terminal(db_session) == []
