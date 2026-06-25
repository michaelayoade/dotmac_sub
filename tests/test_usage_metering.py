"""Metering RADIUS accounting into quota buckets (the FUP/overage gate)."""

from __future__ import annotations

import uuid
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
    UsageAllowance,
)
from app.models.usage import (
    AccountingStatus,
    QuotaBucket,
    RadiusAccountingSession,
)
from app.services.usage import meter_usage_into_quota

_GB = 1024**3


def _offer(db, code, *, allowance=None):
    o = CatalogOffer(
        name=code,
        code=code,
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_cycle=BillingCycle.monthly,
        billing_mode=BillingMode.prepaid,
        is_active=True,
        usage_allowance_id=allowance.id if allowance else None,
    )
    db.add(o)
    db.flush()
    return o


def _sub(db, subscriber, offer):
    s = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=SubscriptionStatus.active,
        billing_mode=offer.billing_mode,
        # start_at left unset → full (un-prorated) allowance, avoids a
        # naive/aware datetime comparison under SQLite.
        next_billing_at=datetime.now(UTC),
    )
    db.add(s)
    db.flush()
    return s


def _session(db, sub, *, gb_in, gb_out):
    db.add(
        RadiusAccountingSession(
            subscription_id=sub.id,
            session_id=uuid.uuid4().hex,
            status_type=AccountingStatus.stop,
            session_start=datetime.now(UTC),
            input_octets=int(gb_in * _GB),
            output_octets=int(gb_out * _GB),
        )
    )
    db.flush()


def _allowance(db, included_gb):
    a = UsageAllowance(name=f"{included_gb}GB", included_gb=included_gb, is_active=True)
    db.add(a)
    db.flush()
    return a


def test_meters_capped_subscription_from_radius(db_session, subscriber):
    allowance = _allowance(db_session, 10)
    offer = _offer(db_session, "capped-10", allowance=allowance)
    sub = _sub(db_session, subscriber, offer)
    _session(db_session, sub, gb_in=3, gb_out=2)  # 5 GB used
    db_session.commit()

    result = meter_usage_into_quota(db_session)
    assert result["metered"] == 1
    assert result["changed_subscription_ids"] == [str(sub.id)]

    bucket = (
        db_session.query(QuotaBucket)
        .filter(QuotaBucket.subscription_id == sub.id)
        .one()
    )
    assert Decimal(str(bucket.used_gb)) == Decimal("5.00")
    assert Decimal(str(bucket.overage_gb)) == Decimal("0.00")  # 5 <= 10 included


def test_overage_when_over_allowance(db_session, subscriber):
    allowance = _allowance(db_session, 10)
    offer = _offer(db_session, "capped-10b", allowance=allowance)
    sub = _sub(db_session, subscriber, offer)
    _session(db_session, sub, gb_in=8, gb_out=4)  # 12 GB used
    db_session.commit()

    meter_usage_into_quota(db_session)
    bucket = (
        db_session.query(QuotaBucket)
        .filter(QuotaBucket.subscription_id == sub.id)
        .one()
    )
    assert Decimal(str(bucket.used_gb)) == Decimal("12.00")
    assert Decimal(str(bucket.overage_gb)) == Decimal("2.00")  # 12 - 10


def test_uncapped_subscription_is_skipped(db_session, subscriber):
    offer = _offer(db_session, "unlimited")  # no allowance
    sub = _sub(db_session, subscriber, offer)
    _session(db_session, sub, gb_in=50, gb_out=50)
    db_session.commit()

    result = meter_usage_into_quota(db_session)
    assert result["metered"] == 0
    # an unlimited plan never gets a quota bucket → never looks "exhausted"
    assert (
        db_session.query(QuotaBucket)
        .filter(QuotaBucket.subscription_id == sub.id)
        .count()
        == 0
    )


def test_metering_is_idempotent(db_session, subscriber):
    allowance = _allowance(db_session, 10)
    offer = _offer(db_session, "capped-idem", allowance=allowance)
    sub = _sub(db_session, subscriber, offer)
    _session(db_session, sub, gb_in=3, gb_out=0)  # 3 GB
    db_session.commit()

    meter_usage_into_quota(db_session)
    result = meter_usage_into_quota(db_session)  # re-run must not double-count
    bucket = (
        db_session.query(QuotaBucket)
        .filter(QuotaBucket.subscription_id == sub.id)
        .one()
    )
    assert Decimal(str(bucket.used_gb)) == Decimal("3.00")
    assert result["changed_subscription_ids"] == []
