"""Rollover: unused allowance carries into the next period's quota bucket."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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
    UsageAllowance,
)
from app.models.usage import QuotaBucket
from app.services.usage import (
    _period_bounds_for_record,
    _resolve_or_create_quota_bucket,
)


def _setup(db, subscriber, *, rollover):
    allowance = UsageAllowance(
        name="10GB",
        included_gb=10,
        rollover_enabled=rollover,
        is_active=True,
    )
    db.add(allowance)
    db.flush()
    offer = CatalogOffer(
        name="capped",
        code=f"cap-roll-{rollover}",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_cycle=BillingCycle.monthly,
        billing_mode=BillingMode.prepaid,
        is_active=True,
        usage_allowance_id=allowance.id,
    )
    db.add(offer)
    db.flush()
    sub = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=SubscriptionStatus.active,
        billing_mode=offer.billing_mode,
        next_billing_at=datetime.now(UTC),  # start_at unset → no proration
    )
    db.add(sub)
    db.flush()
    return sub


def _prev_bucket(db, sub, period_start, *, used, rollover=Decimal("0")):
    db.add(
        QuotaBucket(
            subscription_id=sub.id,
            period_start=period_start - timedelta(days=30),
            period_end=period_start,  # ends exactly where the new period starts
            included_gb=10,
            used_gb=used,
            rollover_gb=rollover,
            overage_gb=Decimal("0.00"),
        )
    )
    db.flush()


def test_rollover_carries_unused(db_session, subscriber):
    sub = _setup(db_session, subscriber, rollover=True)
    now = datetime.now(UTC)
    period_start, _ = _period_bounds_for_record(now)
    _prev_bucket(db_session, sub, period_start, used=Decimal("4.00"))  # 6 unused
    db_session.commit()

    bucket = _resolve_or_create_quota_bucket(db_session, sub, now)
    assert Decimal(str(bucket.rollover_gb)) == Decimal("6.00")


def test_rollover_capped_at_one_period(db_session, subscriber):
    sub = _setup(db_session, subscriber, rollover=True)
    now = datetime.now(UTC)
    period_start, _ = _period_bounds_for_record(now)
    # 10 included + 10 prior rollover, 0 used → 20 available, capped at 10
    _prev_bucket(
        db_session, sub, period_start, used=Decimal("0.00"), rollover=Decimal("10.00")
    )
    db_session.commit()

    bucket = _resolve_or_create_quota_bucket(db_session, sub, now)
    assert Decimal(str(bucket.rollover_gb)) == Decimal("10.00")


def test_no_rollover_when_disabled(db_session, subscriber):
    sub = _setup(db_session, subscriber, rollover=False)
    now = datetime.now(UTC)
    period_start, _ = _period_bounds_for_record(now)
    _prev_bucket(db_session, sub, period_start, used=Decimal("2.00"))  # 8 unused
    db_session.commit()

    bucket = _resolve_or_create_quota_bucket(db_session, sub, now)
    assert Decimal(str(bucket.rollover_gb)) == Decimal("0.00")
