"""billing_mode drift audit (find_billing_mode_inconsistencies)."""

from app.models.catalog import (
    AccessType,
    BillingMode,
    CatalogOffer,
    PriceBasis,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.subscriber import Subscriber
from app.services.billing_mode_audit import find_billing_mode_inconsistencies


def _offer(db, mode: BillingMode) -> CatalogOffer:
    offer = CatalogOffer(
        name=f"Offer {mode.value}",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_mode=mode,
    )
    db.add(offer)
    db.flush()
    return offer


def _subscriber(db, mode: BillingMode, email: str) -> Subscriber:
    sub = Subscriber(first_name="A", last_name="B", email=email, billing_mode=mode)
    db.add(sub)
    db.flush()
    return sub


def _subscription(db, subscriber, offer, mode: BillingMode) -> Subscription:
    s = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=SubscriptionStatus.active,
        billing_mode=mode,
    )
    db.add(s)
    db.flush()
    return s


def test_consistent_account_has_no_issues(db_session):
    sub = _subscriber(db_session, BillingMode.prepaid, "ok@example.com")
    offer = _offer(db_session, BillingMode.prepaid)
    _subscription(db_session, sub, offer, BillingMode.prepaid)
    db_session.commit()
    issues = [
        i
        for i in find_billing_mode_inconsistencies(db_session)
        if i["subscriber_id"] == str(sub.id)
    ]
    assert issues == []


def test_subscription_vs_account_drift_flagged(db_session):
    sub = _subscriber(db_session, BillingMode.prepaid, "drift@example.com")
    offer = _offer(db_session, BillingMode.postpaid)
    _subscription(db_session, sub, offer, BillingMode.postpaid)
    db_session.commit()
    issues = find_billing_mode_inconsistencies(db_session)
    kinds = {i["issue"] for i in issues if i["subscriber_id"] == str(sub.id)}
    assert "subscription_vs_account" in kinds  # sub postpaid vs account prepaid


def test_subscription_vs_offer_drift_flagged(db_session):
    sub = _subscriber(db_session, BillingMode.prepaid, "offerdrift@example.com")
    offer = _offer(db_session, BillingMode.postpaid)
    _subscription(db_session, sub, offer, BillingMode.prepaid)
    db_session.commit()
    issues = find_billing_mode_inconsistencies(db_session)
    kinds = {i["issue"] for i in issues if i["subscriber_id"] == str(sub.id)}
    assert "subscription_vs_offer" in kinds  # sub prepaid vs offer postpaid


def test_mixed_mode_account_flagged(db_session):
    sub = _subscriber(db_session, BillingMode.prepaid, "mixed@example.com")
    prepaid_offer = _offer(db_session, BillingMode.prepaid)
    postpaid_offer = _offer(db_session, BillingMode.postpaid)
    # Two active subscriptions of different modes (drift state reachable via
    # Import / migration / direct writes that bypass enforce_single_active).
    _subscription(db_session, sub, prepaid_offer, BillingMode.prepaid)
    _subscription(db_session, sub, postpaid_offer, BillingMode.postpaid)
    db_session.commit()
    issues = find_billing_mode_inconsistencies(db_session)
    mixed = [
        i
        for i in issues
        if i["subscriber_id"] == str(sub.id) and i["issue"] == "mixed_mode_account"
    ]
    assert len(mixed) == 1
    assert mixed[0]["modes"] == ["postpaid", "prepaid"]
