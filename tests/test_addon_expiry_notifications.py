"""Bundle-expiry notification path: the daily task selects bundles lapsing
within 24h and emits usage.addon_expiring; the notification handler fans that
out to push + email."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from app.models.catalog import (
    AccessType,
    AddOn,
    AddOnType,
    BillingCycle,
    BillingMode,
    CatalogOffer,
    PriceBasis,
    ServiceType,
    Subscription,
    SubscriptionAddOn,
    SubscriptionStatus,
)
from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationTemplate,
)
from app.services.events.handlers.notification import NotificationHandler
from app.services.events.types import Event, EventType


def _make_bundle_purchase(db_session, subscriber, *, end_at: datetime):
    offer = CatalogOffer(
        name="Unlimited Lite",
        code="unlimited-lite",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_cycle=BillingCycle.monthly,
        billing_mode=BillingMode.prepaid,
    )
    db_session.add(offer)
    db_session.flush()
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=SubscriptionStatus.active,
        billing_mode=offer.billing_mode,
        start_at=datetime.now(UTC),
        next_billing_at=datetime.now(UTC),
    )
    add_on = AddOn(
        name="5GB Booster",
        addon_type=AddOnType.custom,
        grant_gb=5,
        is_active=True,
    )
    db_session.add_all([subscription, add_on])
    db_session.flush()
    sub_add_on = SubscriptionAddOn(
        subscription_id=subscription.id,
        add_on_id=add_on.id,
        quantity=1,
        start_at=datetime.now(UTC),
        end_at=end_at,
    )
    db_session.add(sub_add_on)
    db_session.commit()
    return subscription, add_on, sub_add_on


def test_notify_expiring_data_bundles_emits_within_24h_window(
    db_session, subscriber, monkeypatch
):
    _make_bundle_purchase(
        db_session, subscriber, end_at=datetime.now(UTC) + timedelta(hours=2)
    )

    events = []

    def _capture(session, event_type, payload, **kwargs):
        events.append((event_type, payload))

    monkeypatch.setattr(db_session, "close", lambda: None)
    with (
        patch("app.services.db_session_adapter.SessionLocal", return_value=db_session),
        patch("app.services.events.emit_event", _capture),
    ):
        from app.tasks.usage import notify_expiring_data_bundles

        result = notify_expiring_data_bundles()

    assert result == {"notified": 1}
    assert len(events) == 1
    event_type, payload = events[0]
    assert event_type == EventType.addon_expiring
    assert payload["addon_name"] == "5GB Booster"
    assert payload["grant_gb"] == "5"
    assert payload["account_id"] == str(subscriber.id)


def test_notify_expiring_data_bundles_skips_outside_window(
    db_session, subscriber, monkeypatch
):
    # Expires in 3 days — outside the 24h warning window.
    _make_bundle_purchase(
        db_session, subscriber, end_at=datetime.now(UTC) + timedelta(days=3)
    )

    events = []
    monkeypatch.setattr(db_session, "close", lambda: None)
    with (
        patch("app.services.db_session_adapter.SessionLocal", return_value=db_session),
        patch(
            "app.services.events.emit_event",
            lambda *a, **k: events.append(a),
        ),
    ):
        from app.tasks.usage import notify_expiring_data_bundles

        result = notify_expiring_data_bundles()

    assert result == {"notified": 0}
    assert events == []


def test_addon_expiring_event_creates_push_and_email_notifications(
    db_session, subscriber
):
    db_session.add_all(
        [
            NotificationTemplate(
                name="Addon Expiring Email",
                code="addon_expiring",
                channel=NotificationChannel.email,
                subject="Bundle expiring",
                body="{addon_name} expires at {expires_at}",
                is_active=True,
            ),
            NotificationTemplate(
                name="Addon Expiring Push",
                code="addon_expiring",
                channel=NotificationChannel.push,
                subject="Bundle expiring",
                body="{addon_name} expires at {expires_at}",
                is_active=True,
            ),
        ]
    )
    db_session.commit()
    handler = NotificationHandler()
    event = Event(
        event_type=EventType.addon_expiring,
        payload={
            "addon_name": "5GB Booster",
            "expires_at": "2026-06-12T10:00:00+00:00",
        },
        subscriber_id=subscriber.id,
    )

    handler.handle(db_session, event)
    db_session.commit()

    notifications = db_session.query(Notification).all()
    assert {row.channel for row in notifications} == {
        NotificationChannel.push,
        NotificationChannel.email,
    }
    assert all(row.subscriber_id == subscriber.id for row in notifications)
    assert all(row.category == "usage" for row in notifications)
    assert any("5GB Booster" in (row.body or "") for row in notifications)
