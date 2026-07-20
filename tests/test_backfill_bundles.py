from app.models.catalog import (
    Subscription,
    SubscriptionBundle,
    SubscriptionStatus,
)
from scripts.migration.backfill_bundles import backfill_bundles


def test_backfill_groups_base_and_ip(db_session, subscriber, catalog_offer):
    base = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        service_description="60 Mbps Fiber",
        status=SubscriptionStatus.active,
        billing_mode="postpaid",
    )
    ip = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        service_description="SLASH 29 IP",
        status=SubscriptionStatus.suspended,
        billing_mode="postpaid",
    )
    db_session.add_all([base, ip])
    db_session.flush()

    stats = backfill_bundles(db_session, commit=False)

    db_session.refresh(base)
    db_session.refresh(ip)
    assert base.bundle_id is not None
    assert ip.bundle_id == base.bundle_id  # base + IP in the same bundle
    bundle = db_session.get(SubscriptionBundle, base.bundle_id)
    assert bundle.anchor_subscription_id == base.id  # anchor is the base internet
    assert stats["bundles_created"] >= 1


def test_backfill_skips_ip_only_account(db_session, subscriber, catalog_offer):
    # An account with only an IP sub (no base internet) is not bundled.
    ip = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        service_description="SLASH 30 IP",
        status=SubscriptionStatus.active,
        billing_mode="postpaid",
    )
    db_session.add(ip)
    db_session.flush()

    backfill_bundles(db_session, commit=False)

    db_session.refresh(ip)
    assert ip.bundle_id is None
