from __future__ import annotations

import uuid

from app.models.catalog import (
    AccessType,
    BillingMode,
    CatalogOffer,
    OfferStatus,
    PriceBasis,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.notification import NotificationChannel, NotificationStatus
from app.models.subscriber import Reseller, Subscriber, SubscriberStatus
from app.schemas.notification import NotificationCreate
from app.services.account_lifecycle import (
    disable_subscription,
    transition_account_status,
    transition_subscription_status,
)
from app.services.communication_intents import (
    CommunicationClass,
    CommunicationIntent,
    submit,
    suppress,
    unsuppress,
)
from app.services.notification import notifications


def _subscriber(db_session, *, reseller=None, marketing_opt_in=True):
    subscriber = Subscriber(
        first_name="Lifecycle",
        last_name="Customer",
        email=f"lifecycle-{uuid.uuid4().hex}@example.com",
        phone="+2348012345678",
        status=SubscriberStatus.active,
        is_active=True,
        reseller=reseller,
        marketing_opt_in=marketing_opt_in,
    )
    db_session.add(subscriber)
    db_session.flush()
    return subscriber


def _subscription(db_session, subscriber, *, status=SubscriptionStatus.active):
    offer = CatalogOffer(
        name=f"Lifecycle offer {uuid.uuid4().hex[:8]}",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        status=OfferStatus.active,
        is_active=True,
    )
    db_session.add(offer)
    db_session.flush()
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=status,
        billing_mode=BillingMode.prepaid,
    )
    db_session.add(subscription)
    db_session.flush()
    return subscription


def _intent(subscriber, **overrides):
    values = {
        "subscriber_id": subscriber.id,
        "event_type": "service.changed",
        "category": "service",
        "subject": "Service update",
        "body": "Your service changed.",
        "channels": (NotificationChannel.email,),
    }
    values.update(overrides)
    return CommunicationIntent(**values)


def test_intent_expands_active_reseller_and_preserves_disabled_customer_gate(
    db_session,
):
    reseller = Reseller(
        name="Partner",
        contact_email="partner@example.com",
        is_active=True,
        is_house=False,
    )
    db_session.add(reseller)
    subscriber = _subscriber(db_session, reseller=reseller)

    first = submit(db_session, _intent(subscriber))
    subscriber.status = SubscriberStatus.disabled
    subscriber.is_active = False
    second = submit(
        db_session,
        _intent(subscriber, event_type="service.disabled"),
    )

    assert [item.recipient for item in first.queued] == [
        subscriber.email,
        "partner@example.com",
    ]
    subscriber_row, reseller_row = second.deliveries
    assert subscriber_row.status == NotificationStatus.canceled
    assert "account notification status policy" in subscriber_row.last_error
    assert reseller_row.status == NotificationStatus.queued
    assert reseller_row.recipient == "partner@example.com"


def test_durable_suppression_applies_to_intents_and_legacy_queue(db_session):
    subscriber = _subscriber(db_session)
    row = suppress(
        db_session,
        subscriber_id=subscriber.id,
        channel=NotificationChannel.email,
        category="service",
        reason="hard_bounce",
        source="provider:webhook",
    )

    result = submit(db_session, _intent(subscriber))
    legacy = notifications.queue_customer_notification(
        db_session,
        NotificationCreate(
            subscriber_id=subscriber.id,
            channel=NotificationChannel.email,
            recipient=subscriber.email,
            category="service",
            event_type="legacy.service",
        ),
    )
    unsuppress(db_session, row.id)
    allowed = submit(db_session, _intent(subscriber, event_type="service.restored"))

    assert result.queued == ()
    assert result.suppressed == ("subscriber:email:hard_bounce",)
    assert legacy.status == NotificationStatus.canceled
    assert legacy.last_error == "Suppressed by communication ledger: hard_bounce"
    assert len(allowed.queued) == 1


def test_marketing_opt_out_suppresses_subscriber_and_reseller(db_session):
    reseller = Reseller(
        name="Marketing Partner",
        contact_email="marketing-partner@example.com",
        is_active=True,
        is_house=False,
    )
    db_session.add(reseller)
    subscriber = _subscriber(
        db_session,
        reseller=reseller,
        marketing_opt_in=False,
    )

    result = submit(
        db_session,
        _intent(
            subscriber,
            communication_class=CommunicationClass.marketing,
            category="marketing",
        ),
    )

    assert result.queued == ()
    assert result.suppressed == ("marketing_opt_out",)


def test_lifecycle_disable_and_lockless_admin_repair_are_canonical(db_session):
    subscriber = _subscriber(db_session)
    subscription = _subscription(
        db_session,
        subscriber,
        status=SubscriptionStatus.suspended,
    )

    repaired = transition_subscription_status(
        db_session,
        str(subscription.id),
        SubscriptionStatus.active,
        reason="Repair legacy lockless suspension",
        source="admin:test",
        emit=False,
    )
    disabled = disable_subscription(
        db_session,
        str(subscription.id),
        reason="Account closed",
        source="admin:test",
        emit=False,
    )

    assert repaired is True
    assert disabled is True
    assert subscription.status == SubscriptionStatus.disabled
    assert subscription.end_at is not None
    assert subscription.canceled_at is not None
    assert subscriber.status == SubscriberStatus.disabled
    assert subscriber.is_active is False


def test_admin_transition_does_not_bypass_duplicate_login_restore_guard(db_session):
    subscriber = _subscriber(db_session)
    active = _subscription(db_session, subscriber)
    suspended = _subscription(
        db_session,
        subscriber,
        status=SubscriptionStatus.suspended,
    )
    active.login = "shared-lifecycle-login"
    suspended.login = "shared-lifecycle-login"
    db_session.flush()

    restored = transition_subscription_status(
        db_session,
        str(suspended.id),
        SubscriptionStatus.active,
        reason="Administrative restore",
        source="admin:test",
        emit=False,
    )

    assert restored is False
    assert active.status == SubscriptionStatus.active
    assert suspended.status == SubscriptionStatus.suspended


def test_explicit_account_suspension_is_inactive_and_reversible(db_session):
    subscriber = _subscriber(db_session)
    subscription = _subscription(db_session, subscriber)

    transition_account_status(
        db_session,
        str(subscriber.id),
        SubscriberStatus.suspended,
        reason="Administrative deactivation",
        source="admin:test",
        emit=False,
    )

    assert subscriber.lifecycle_override_status == SubscriberStatus.suspended
    assert subscriber.status == SubscriberStatus.suspended
    assert subscriber.is_active is False
    assert subscription.status == SubscriptionStatus.suspended

    transition_account_status(
        db_session,
        str(subscriber.id),
        SubscriberStatus.active,
        reason="Administrative reactivation",
        source="admin:test",
        emit=False,
    )

    assert subscriber.lifecycle_override_status is None
    assert subscriber.status == SubscriberStatus.active
    assert subscriber.is_active is True
    assert subscription.status == SubscriptionStatus.active
