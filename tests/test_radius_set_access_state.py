"""Local access-state ownership; external groups belong to radius_population."""

from __future__ import annotations

from app.models.catalog import (
    AccessCredential,
    AccessState,
    Subscription,
    SubscriptionStatus,
)
from app.services.radius_access_state import set_subscription_access_state


def _seed_subscription(
    db,
    subscriber,
    catalog_offer,
    *,
    username: str,
    status: SubscriptionStatus = SubscriptionStatus.active,
):
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=status,
    )
    db.add(subscription)
    db.flush()
    db.add(
        AccessCredential(
            subscriber_id=subscriber.id,
            username=username,
            is_active=True,
        )
    )
    db.commit()
    return subscription


def test_updates_local_state_without_external_write(
    db_session, subscriber, catalog_offer
):
    subscription = _seed_subscription(
        db_session, subscriber, catalog_offer, username="local-state"
    )

    result = set_subscription_access_state(
        db_session, str(subscription.id), AccessState.active
    )

    db_session.refresh(subscription)
    assert subscription.access_state == "active"
    assert result == {
        "credentials": 1,
        "external_rows_written": 0,
        "external_rows_deleted": 0,
        "aggregate_state": "active",
    }


def test_subscriber_aggregate_remains_most_permissive(
    db_session, subscriber, catalog_offer
):
    active = _seed_subscription(
        db_session, subscriber, catalog_offer, username="aggregate-state"
    )
    terminated = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.canceled,
    )
    db_session.add(terminated)
    db_session.commit()

    set_subscription_access_state(db_session, str(active.id), AccessState.active)
    result = set_subscription_access_state(
        db_session, str(terminated.id), AccessState.terminated
    )

    assert result["aggregate_state"] == "active"


def test_no_credentials_is_a_local_only_noop(db_session, subscriber, catalog_offer):
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.active,
    )
    db_session.add(subscription)
    db_session.commit()

    result = set_subscription_access_state(
        db_session, str(subscription.id), AccessState.active
    )

    assert result["credentials"] == 0
    assert result["aggregate_state"] == "active"


def test_missing_subscription_returns_skip(db_session):
    result = set_subscription_access_state(
        db_session,
        "00000000-0000-0000-0000-000000000000",
        AccessState.active,
    )
    assert result == {
        "credentials": 0,
        "external_rows_written": 0,
        "external_rows_deleted": 0,
        "aggregate_state": None,
    }
