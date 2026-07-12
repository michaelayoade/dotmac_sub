from app.models.catalog import Subscription, SubscriptionStatus
from app.services import bundles


def test_reconcile_converges_divergent_member(
    db_session, subscriber, subscription, catalog_offer
):
    from app.models.enforcement_lock import EnforcementReason
    from app.services.account_lifecycle import suspend_subscription

    anchor = subscription  # active
    ip = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.active,
        billing_mode=subscription.billing_mode,
    )
    db_session.add(ip)
    db_session.flush()
    b = bundles.create_bundle(db_session, str(subscriber.id), str(anchor.id))
    bundles.add_member(db_session, str(b.id), str(anchor.id))
    bundles.add_member(db_session, str(b.id), str(ip.id))
    # realistic divergence: the IP member is suspended (with an overdue lock)
    # while the anchor stays active.
    suspend_subscription(
        db_session, str(ip.id), reason=EnforcementReason.overdue, source="test"
    )
    db_session.refresh(ip)
    assert ip.status == SubscriptionStatus.suspended

    stats = bundles.reconcile_bundle_states(db_session, str(b.id))
    db_session.refresh(ip)
    # anchor active -> the divergent suspended member is restored to active
    assert ip.status == SubscriptionStatus.active
    assert stats["members_converged"] == 1


def test_suspend_account_skips_dedicated_bundle(
    db_session, subscriber, subscription, catalog_offer
):
    from app.services.collections._core import (
        _account_has_dedicated_bundle,
        _suspend_account,
    )

    subscription.status = SubscriptionStatus.active
    db_session.flush()
    b = bundles.create_bundle(db_session, str(subscriber.id), str(subscription.id))
    bundles.add_member(db_session, str(b.id), str(subscription.id))
    catalog_offer.plan_family = "dedicated"
    db_session.flush()
    assert bundles.recompute_is_dedicated(db_session, str(b.id)) is True
    assert _account_has_dedicated_bundle(db_session, subscriber.id) is True

    result = _suspend_account(db_session, str(subscriber.id))
    db_session.refresh(subscription)
    assert result is False  # dedicated bundle -> hands-off
    assert subscription.status == SubscriptionStatus.active


def test_suspend_account_suspends_non_dedicated_bundle(
    db_session, subscriber, subscription, catalog_offer
):
    from app.services.collections._core import _suspend_account

    subscription.status = SubscriptionStatus.active
    db_session.flush()
    b = bundles.create_bundle(db_session, str(subscriber.id), str(subscription.id))
    bundles.add_member(db_session, str(b.id), str(subscription.id))

    result = _suspend_account(db_session, str(subscriber.id))
    db_session.refresh(subscription)
    assert result is True
    assert subscription.status == SubscriptionStatus.suspended


def test_run_bundle_reconcile_task(db_session, monkeypatch):
    from app.services.collections import scheduled
    from app.tasks import collections as ctask

    monkeypatch.setattr(scheduled, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "commit", lambda: None)
    monkeypatch.setattr(db_session, "close", lambda: None)

    result = ctask.run_bundle_reconcile()
    assert "bundles_scanned" in result
    assert "members_converged" in result
