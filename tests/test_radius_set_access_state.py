"""Subscription access state is derived by the lifecycle owner."""

from __future__ import annotations

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.enforcement_lock import (
    AccessRestrictionMode,
    EnforcementLock,
    EnforcementReason,
)
from app.models.subscriber import SubscriberStatus
from app.services.account_lifecycle import compute_account_status


def _subscription(db, subscriber, offer, *, status: SubscriptionStatus):
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=status,
    )
    db.add(subscription)
    db.flush()
    return subscription


def test_lifecycle_projects_active_child_access(db_session, subscriber, catalog_offer):
    subscriber.status = SubscriberStatus.blocked
    subscription = _subscription(
        db_session,
        subscriber,
        catalog_offer,
        status=SubscriptionStatus.active,
    )

    status = compute_account_status(db_session, str(subscriber.id))

    assert status is SubscriberStatus.active
    assert subscription.access_state == "active"


def test_lifecycle_projects_active_lock_as_restricted(
    db_session,
    subscriber,
    catalog_offer,
):
    subscription = _subscription(
        db_session,
        subscriber,
        catalog_offer,
        status=SubscriptionStatus.active,
    )
    db_session.add(
        EnforcementLock(
            subscriber_id=subscriber.id,
            subscription_id=subscription.id,
            reason=EnforcementReason.admin,
            access_mode=AccessRestrictionMode.hard_reject,
            source="test:access-owner",
            is_active=True,
        )
    )

    compute_account_status(db_session, str(subscriber.id))

    assert subscription.access_state == "suspended"


def test_lifecycle_projects_terminal_child_access(
    db_session,
    subscriber,
    catalog_offer,
):
    subscription = _subscription(
        db_session,
        subscriber,
        catalog_offer,
        status=SubscriptionStatus.canceled,
    )

    compute_account_status(db_session, str(subscriber.id))

    assert subscription.access_state == "terminated"
