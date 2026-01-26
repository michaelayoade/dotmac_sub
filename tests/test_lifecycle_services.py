"""Tests for lifecycle service."""

from app.models.lifecycle import LifecycleEventType
from app.schemas.lifecycle import SubscriptionLifecycleEventCreate, SubscriptionLifecycleEventUpdate
from app.services import lifecycle as lifecycle_service


def test_create_lifecycle_event(db_session, subscription):
    """Test creating a subscription lifecycle event."""
    event = lifecycle_service.subscription_lifecycle_events.create(
        db_session,
        SubscriptionLifecycleEventCreate(
            subscription_id=subscription.id,
            event_type=LifecycleEventType.activate,
            notes="Subscription activated",
        ),
    )
    assert event.subscription_id == subscription.id
    assert event.event_type == LifecycleEventType.activate
    assert event.notes == "Subscription activated"


def test_list_lifecycle_events_by_subscription(db_session, subscription):
    """Test listing lifecycle events by subscription."""
    # Create multiple events
    lifecycle_service.subscription_lifecycle_events.create(
        db_session,
        SubscriptionLifecycleEventCreate(
            subscription_id=subscription.id,
            event_type=LifecycleEventType.activate,
        ),
    )
    lifecycle_service.subscription_lifecycle_events.create(
        db_session,
        SubscriptionLifecycleEventCreate(
            subscription_id=subscription.id,
            event_type=LifecycleEventType.suspend,
        ),
    )

    events = lifecycle_service.subscription_lifecycle_events.list(
        db_session,
        subscription_id=str(subscription.id),
        event_type=None,
        order_by="created_at",
        order_dir="asc",
        limit=10,
        offset=0,
    )
    assert len(events) >= 2
    assert all(e.subscription_id == subscription.id for e in events)


def test_list_lifecycle_events_by_type(db_session, subscription):
    """Test listing lifecycle events filtered by type."""
    lifecycle_service.subscription_lifecycle_events.create(
        db_session,
        SubscriptionLifecycleEventCreate(
            subscription_id=subscription.id,
            event_type=LifecycleEventType.activate,
        ),
    )
    lifecycle_service.subscription_lifecycle_events.create(
        db_session,
        SubscriptionLifecycleEventCreate(
            subscription_id=subscription.id,
            event_type=LifecycleEventType.suspend,
        ),
    )

    activate_events = lifecycle_service.subscription_lifecycle_events.list(
        db_session,
        subscription_id=None,
        event_type="activate",
        order_by="created_at",
        order_dir="asc",
        limit=10,
        offset=0,
    )
    assert all(e.event_type == LifecycleEventType.activate for e in activate_events)


def test_update_lifecycle_event(db_session, subscription):
    """Test updating a lifecycle event."""
    event = lifecycle_service.subscription_lifecycle_events.create(
        db_session,
        SubscriptionLifecycleEventCreate(
            subscription_id=subscription.id,
            event_type=LifecycleEventType.activate,
            notes="Initial note",
        ),
    )

    updated = lifecycle_service.subscription_lifecycle_events.update(
        db_session,
        str(event.id),
        SubscriptionLifecycleEventUpdate(notes="Updated note"),
    )
    assert updated.notes == "Updated note"


def test_delete_lifecycle_event(db_session, subscription):
    """Test deleting a lifecycle event."""
    import pytest
    from fastapi import HTTPException

    event = lifecycle_service.subscription_lifecycle_events.create(
        db_session,
        SubscriptionLifecycleEventCreate(
            subscription_id=subscription.id,
            event_type=LifecycleEventType.activate,
        ),
    )
    event_id = event.id

    lifecycle_service.subscription_lifecycle_events.delete(db_session, str(event_id))

    # Verify event is deleted
    with pytest.raises(HTTPException) as exc_info:
        lifecycle_service.subscription_lifecycle_events.get(db_session, str(event_id))
    assert exc_info.value.status_code == 404


def test_get_lifecycle_event(db_session, subscription):
    """Test getting a lifecycle event by ID."""
    event = lifecycle_service.subscription_lifecycle_events.create(
        db_session,
        SubscriptionLifecycleEventCreate(
            subscription_id=subscription.id,
            event_type=LifecycleEventType.cancel,
            notes="Service cancelled",
        ),
    )

    fetched = lifecycle_service.subscription_lifecycle_events.get(db_session, str(event.id))
    assert fetched is not None
    assert fetched.id == event.id
    assert fetched.event_type == LifecycleEventType.cancel
