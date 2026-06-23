"""Step 2d (#306): the lifecycle-transition shadow-diff hook.

Observability only — the LifecycleHandler runs the READ-ONLY connectivity
shadow diff after recording a transition, and a failure in the diff must never
block the lifecycle audit write. No writer migration here.
"""

from __future__ import annotations

from datetime import UTC, datetime

import app.services.events.handlers.lifecycle as lifecycle_mod
from app.models.catalog import Subscription, SubscriptionStatus
from app.models.lifecycle import SubscriptionLifecycleEvent
from app.services.events.handlers.lifecycle import LifecycleHandler
from app.services.events.types import Event, EventType


def _active_sub(db, subscriber, catalog_offer) -> Subscription:
    sub = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.active,
        billing_mode=catalog_offer.billing_mode,
        start_at=datetime.now(UTC),
    )
    db.add(sub)
    db.flush()
    return sub


def _suspend_event(sub) -> Event:
    return Event(
        event_type=EventType.subscription_suspended,
        payload={
            "subscription_id": str(sub.id),
            "from_status": "active",
            "to_status": "suspended",
        },
        subscription_id=sub.id,
    )


def _lifecycle_rows(db, sub):
    return (
        db.query(SubscriptionLifecycleEvent)
        .filter(SubscriptionLifecycleEvent.subscription_id == sub.id)
        .all()
    )


def test_hook_records_lifecycle_and_calls_shadow_diff(
    db_session, subscriber, catalog_offer, monkeypatch
):
    sub = _active_sub(db_session, subscriber, catalog_offer)
    seen: list = []
    monkeypatch.setattr(
        lifecycle_mod,
        "connectivity_shadow_diff",
        lambda db, subscriber_id: seen.append(subscriber_id),
    )

    LifecycleHandler().handle(db_session, _suspend_event(sub))

    # Audit record persisted...
    rows = _lifecycle_rows(db_session, sub)
    assert len(rows) == 1
    assert rows[0].to_status == SubscriptionStatus.suspended
    # ...and the shadow diff ran for this subscriber.
    assert seen == [subscriber.id]


def test_shadow_diff_failure_is_isolated(
    db_session, subscriber, catalog_offer, monkeypatch
):
    sub = _active_sub(db_session, subscriber, catalog_offer)

    def _boom(db, subscriber_id):
        raise RuntimeError("shadow diff blew up")

    monkeypatch.setattr(lifecycle_mod, "connectivity_shadow_diff", _boom)

    # Must not raise — failure is swallowed/logged.
    LifecycleHandler().handle(db_session, _suspend_event(sub))

    # The audit write survived the swallowed failure, and the session is still
    # usable afterwards (the savepoint rolled back only the diff).
    rows = _lifecycle_rows(db_session, sub)
    assert len(rows) == 1
    assert db_session.query(Subscription).filter(Subscription.id == sub.id).one()


def test_real_shadow_diff_runs_read_only(db_session, subscriber, catalog_offer):
    """End-to-end with the real (read-only) shadow diff: no error, record kept,
    and nothing on the subscription is mutated by the observation."""
    sub = _active_sub(db_session, subscriber, catalog_offer)
    before_status = sub.status
    before_ip = sub.ipv4_address

    LifecycleHandler().handle(db_session, _suspend_event(sub))

    assert len(_lifecycle_rows(db_session, sub)) == 1
    db_session.refresh(sub)
    assert sub.status == before_status  # handler records, does not transition
    assert sub.ipv4_address == before_ip  # shadow diff wrote nothing
