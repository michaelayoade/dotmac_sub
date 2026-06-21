"""Prepaid balance enforcement must only suspend PREPAID services.

A subscriber can hold both a prepaid and a postpaid service. Prepaid lapses on
balance exhaustion; postpaid lapses only via dunning on overdue invoices. So a
low prepaid balance must never cut the postpaid service on the same account
(the leaky guard behind ~17 postpaid subs found suspended with a prepaid reason).
Dunning, by contrast, suspends the whole account on arrears.
"""

from app.models.catalog import BillingMode, Subscription, SubscriptionStatus
from app.models.enforcement_lock import EnforcementReason
from app.services.collections._core import _suspend_account


def _mk(db, subscriber, offer, mode):
    # Build via the ORM directly: the service-layer validator forbids a second
    # active subscription per account, but this account legitimately holds two
    # (prepaid + postpaid) for the scenario under test.
    sub = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=SubscriptionStatus.active,
        billing_mode=mode,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def test_prepaid_enforcement_spares_postpaid_service(
    db_session, subscriber, catalog_offer
):
    prepaid = _mk(db_session, subscriber, catalog_offer, BillingMode.prepaid)
    postpaid = _mk(db_session, subscriber, catalog_offer, BillingMode.postpaid)

    _suspend_account(
        db_session,
        str(subscriber.id),
        reason=EnforcementReason.prepaid,
        source="prepaid_enforcement",
        only_billing_mode=BillingMode.prepaid,
    )

    db_session.refresh(prepaid)
    db_session.refresh(postpaid)
    assert prepaid.status == SubscriptionStatus.suspended
    assert postpaid.status == SubscriptionStatus.active  # spared


def test_dunning_suspend_still_covers_whole_account(
    db_session, subscriber, catalog_offer
):
    prepaid = _mk(db_session, subscriber, catalog_offer, BillingMode.prepaid)
    postpaid = _mk(db_session, subscriber, catalog_offer, BillingMode.postpaid)

    # No mode filter (the dunning/overdue path): suspends the whole account.
    _suspend_account(
        db_session,
        str(subscriber.id),
        reason=EnforcementReason.overdue,
        source="dunning",
    )

    db_session.refresh(prepaid)
    db_session.refresh(postpaid)
    assert prepaid.status == SubscriptionStatus.suspended
    assert postpaid.status == SubscriptionStatus.suspended
