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
