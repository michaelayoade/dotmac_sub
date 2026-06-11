"""Data top-ups: Splynx cap_tariff import + grant-to-bucket on purchase."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.catalog import (
    AccessType,
    AddOn,
    AddOnType,
    BillingCycle,
    BillingMode,
    CatalogOffer,
    OfferAddOn,
    PriceBasis,
    ServiceType,
    Subscription,
    SubscriptionAddOn,
    SubscriptionStatus,
    UsageAllowance,
)
from app.models.usage import AccountingStatus, QuotaBucket, RadiusAccountingSession
from app.services.migrations.sync_data_topups_from_splynx import import_data_topups
from app.services.usage import grant_data_topup, meter_usage_into_quota

_GB = 1024**3


def _offer(db, code, splynx_tariff_id, *, allowance=None):
    o = CatalogOffer(
        name=code,
        code=code,
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_cycle=BillingCycle.monthly,
        billing_mode=BillingMode.prepaid,
        is_active=True,
        splynx_tariff_id=splynx_tariff_id,
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
        next_billing_at=datetime.now(UTC),
    )
    db.add(s)
    db.flush()
    return s


def _cap(id, tariff_id, title, gb, price, *, deleted="0", unit="gb", validity="1"):
    return {
        "id": id,
        "tariff_id": tariff_id,
        "title": title,
        "amount": gb,
        "amount_in": unit,
        "price": Decimal(str(price)),
        "deleted": deleted,
        "validity": validity,
    }


def test_imports_cap_tariff_as_topups(db_session):
    offer = _offer(db_session, "Capped 200", 56)
    db_session.commit()
    rows = [
        _cap(3, 56, "10GB", 10, 3000, validity="end_of_period"),
        _cap(6, 56, "250 Capped Top Up", 250, 30000, validity="1"),  # 1 period
        _cap(2, 56, "old", 20, 6000, deleted="1"),  # deleted → skip
        _cap(9, 999, "no offer", 10, 3000),  # no matching offer
    ]
    summary = import_data_topups(db_session, rows)
    assert summary == {"topups": 2, "skipped": 1, "no_offer": 1}

    tengb = db_session.query(AddOn).filter_by(splynx_source="cap_tariff:3").one()
    assert tengb.grant_gb == 10
    assert tengb.validity_days is None  # end_of_period
    big = db_session.query(AddOn).filter_by(splynx_source="cap_tariff:6").one()
    assert big.validity_days == 30  # 1 period → ~30 days
    # linked to the plan so its customers can buy it
    assert (
        db_session.query(OfferAddOn)
        .filter_by(offer_id=offer.id, add_on_id=tengb.id)
        .count()
        == 1
    )


def _topup_addon(db, gb, *, validity_days=None):
    a = AddOn(
        name=f"{gb}GB",
        addon_type=AddOnType.custom,
        is_active=True,
        grant_gb=gb,
        validity_days=validity_days,
    )
    db.add(a)
    db.flush()
    return a


def _buy(db, sub, add_on, *, quantity=1, start_at=None):
    sa = SubscriptionAddOn(
        subscription_id=sub.id,
        add_on_id=add_on.id,
        quantity=quantity,
        start_at=start_at or datetime.now(UTC),
    )
    db.add(sa)
    db.flush()
    return sa


def test_grant_credits_quota_bucket(db_session, subscriber):
    offer = _offer(db_session, "Capped", 56)
    sub = _sub(db_session, subscriber, offer)
    db_session.commit()

    a10 = _topup_addon(db_session, 10)
    grant_data_topup(db_session, sub, _buy(db_session, sub, a10), a10)
    a5 = _topup_addon(db_session, 5)
    bucket = grant_data_topup(db_session, sub, _buy(db_session, sub, a5), a5)
    assert Decimal(str(bucket.topup_gb)) == Decimal("15.00")  # accumulates


def test_expired_topup_is_not_counted(db_session, subscriber):
    allowance = UsageAllowance(name="10GB", included_gb=10, is_active=True)
    db_session.add(allowance)
    db_session.flush()
    offer = _offer(db_session, "Capped", 56, allowance=allowance)
    sub = _sub(db_session, subscriber, offer)
    db_session.commit()

    add_on = _topup_addon(db_session, 10, validity_days=7)
    sa = _buy(db_session, sub, add_on)
    grant_data_topup(db_session, sub, sa, add_on)
    bucket = db_session.query(QuotaBucket).filter_by(subscription_id=sub.id).one()
    assert Decimal(str(bucket.topup_gb)) == Decimal("10.00")

    # expire it, re-meter → drops out
    sa.end_at = datetime.now(UTC) - timedelta(days=1)
    db_session.flush()
    meter_usage_into_quota(db_session)
    assert Decimal(str(bucket.topup_gb)) == Decimal("0.00")


def test_topup_offsets_overage_in_metering(db_session, subscriber):
    allowance = UsageAllowance(name="10GB", included_gb=10, is_active=True)
    db_session.add(allowance)
    db_session.flush()
    offer = _offer(db_session, "Capped10", 56, allowance=allowance)
    sub = _sub(db_session, subscriber, offer)
    db_session.add(
        RadiusAccountingSession(
            subscription_id=sub.id,
            session_id=uuid.uuid4().hex,
            status_type=AccountingStatus.stop,
            session_start=datetime.now(UTC),
            input_octets=12 * _GB,  # 12 GB used over a 10 GB cap
            output_octets=0,
        )
    )
    db_session.commit()

    # before a top-up: 2 GB overage
    meter_usage_into_quota(db_session)
    bucket = db_session.query(QuotaBucket).filter_by(subscription_id=sub.id).one()
    assert Decimal(str(bucket.overage_gb)) == Decimal("2.00")

    # buy 5 GB → now within allowance, no overage (bucket is the same
    # in-session object the metering mutates)
    a5 = _topup_addon(db_session, 5)
    grant_data_topup(db_session, sub, _buy(db_session, sub, a5), a5)
    meter_usage_into_quota(db_session)
    assert Decimal(str(bucket.overage_gb)) == Decimal("0.00")


def test_granted_topup_visible_in_customer_quota_response(db_session, subscriber):
    """A purchased bundle must be visible to the customer immediately via the
    /me/quota-buckets payload (topup_gb), not after the next metering run."""
    from app.services.usage import quota_buckets

    offer = _offer(db_session, "Capped", 56)
    sub = _sub(db_session, subscriber, offer)
    db_session.commit()

    add_on = _topup_addon(db_session, 5)
    grant_data_topup(db_session, sub, _buy(db_session, sub, add_on), add_on)
    db_session.commit()

    response = quota_buckets.list_response_for_subscriber(
        db_session, str(subscriber.id), limit=10, offset=0
    )
    items = response["items"]
    assert len(items) == 1
    assert Decimal(str(items[0].topup_gb)) == Decimal("5.00")
