"""FUP auto-lift: usage_exhausted events must carry cap_resets_at so the
enforcement handler stores it and the throttle/block lifts at the period reset.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.models.catalog import (
    AccessType,
    BillingCycle,
    BillingMode,
    CatalogOffer,
    PriceBasis,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.usage import QuotaBucket
from app.services import usage as usage_service
from app.services.events.types import EventType


def _sub(db, subscriber):
    offer = CatalogOffer(
        name="capped",
        code="capped-fup",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_cycle=BillingCycle.monthly,
        billing_mode=BillingMode.prepaid,
        is_active=True,
    )
    db.add(offer)
    db.flush()
    sub = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=SubscriptionStatus.active,
        billing_mode=offer.billing_mode,
        next_billing_at=datetime.now(UTC),
    )
    db.add(sub)
    db.flush()
    return sub


def test_usage_exhausted_carries_cap_resets_at(db_session, subscriber, monkeypatch):
    captured = []
    monkeypatch.setattr(
        usage_service,
        "emit_event",
        lambda db, event_type, payload, **kw: captured.append((event_type, payload)),
    )
    sub = _sub(db_session, subscriber)
    period_end = datetime(2026, 7, 1, tzinfo=UTC)
    bucket = QuotaBucket(
        subscription_id=sub.id,
        period_start=datetime(2026, 6, 1, tzinfo=UTC),
        period_end=period_end,
        included_gb=Decimal("10.00"),
        used_gb=Decimal("0.00"),
        rollover_gb=Decimal("0.00"),
        overage_gb=Decimal("0.00"),
    )
    db_session.add(bucket)
    db_session.commit()

    # cross the included allowance (0 -> 11 over 10)
    usage_service._emit_usage_events(
        db_session, sub, bucket, Decimal("0.00"), Decimal("11.00")
    )

    exhausted = [p for (et, p) in captured if et == EventType.usage_exhausted]
    assert exhausted, "expected a usage_exhausted event"
    # carries the period reset boundary (compare to the stored value — SQLite
    # reads the datetime back naive; Postgres keeps the +00:00 offset).
    assert exhausted[0]["cap_resets_at"] == bucket.period_end.isoformat()
    assert exhausted[0]["cap_resets_at"] is not None
