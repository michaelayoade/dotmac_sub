from app.models.catalog import SubscriptionBundle


def test_bundle_model_and_membership(db_session, subscriber, subscription):
    bundle = SubscriptionBundle(
        subscriber_id=subscriber.id,
        label="Business 100 + /29",
        anchor_subscription_id=subscription.id,
        status="active",
    )
    db_session.add(bundle)
    db_session.flush()
    subscription.bundle_id = bundle.id
    db_session.flush()
    db_session.refresh(subscription)
    assert subscription.bundle_id == bundle.id
    assert bundle.is_dedicated is False  # server default


from app.services import bundles


def test_create_bundle_and_dedicated_flag(
    db_session, subscriber, subscription, catalog_offer
):

    b = bundles.create_bundle(
        db_session, str(subscriber.id), str(subscription.id), label="B"
    )
    bundles.add_member(db_session, str(b.id), str(subscription.id))
    assert [s.id for s in bundles.bundle_members(db_session, str(b.id))] == [
        subscription.id
    ]
    # dedicated marker follows the member offer's plan_family
    catalog_offer.plan_family = "dedicated"
    db_session.flush()
    assert bundles.recompute_is_dedicated(db_session, str(b.id)) is True


def test_suspend_bundle_is_atomic(db_session, subscriber, subscription, catalog_offer):
    from app.models.catalog import Subscription, SubscriptionStatus
    from app.models.enforcement_lock import EnforcementReason

    m2 = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.active,
        billing_mode=subscription.billing_mode,
    )
    db_session.add(m2)
    db_session.flush()
    b = bundles.create_bundle(db_session, str(subscriber.id), str(subscription.id))
    bundles.add_member(db_session, str(b.id), str(subscription.id))
    bundles.add_member(db_session, str(b.id), str(m2.id))

    n = bundles.suspend_bundle(
        db_session, str(b.id), reason=EnforcementReason.overdue, source="test"
    )
    db_session.refresh(subscription)
    db_session.refresh(m2)
    assert n == 2
    assert subscription.status == SubscriptionStatus.suspended
    assert m2.status == SubscriptionStatus.suspended


def test_new_ip_component_joins_existing_bundle(
    db_session, subscriber, subscription, catalog_offer
):
    from app.models.catalog import Subscription, SubscriptionStatus

    b = bundles.create_bundle(db_session, str(subscriber.id), str(subscription.id))
    bundles.add_member(db_session, str(b.id), str(subscription.id))
    ip = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        service_description="SLASH 29 IP",
        status=SubscriptionStatus.active,
        billing_mode=subscription.billing_mode,
    )
    db_session.add(ip)
    db_session.flush()

    bundles.attach_component(db_session, str(subscriber.id), str(ip.id))
    db_session.refresh(ip)
    assert ip.bundle_id == b.id


def test_attach_component_creates_bundle_anchored_on_base(
    db_session, subscriber, subscription, catalog_offer
):
    from app.models.catalog import Subscription, SubscriptionStatus

    subscription.service_description = "60 Mbps Fiber"
    subscription.status = SubscriptionStatus.active
    db_session.flush()
    ip = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        service_description="SLASH 29 IP",
        status=SubscriptionStatus.active,
        billing_mode=subscription.billing_mode,
    )
    db_session.add(ip)
    db_session.flush()

    bundles.attach_component(db_session, str(subscriber.id), str(ip.id))
    db_session.refresh(ip)
    db_session.refresh(subscription)
    assert ip.bundle_id is not None
    assert subscription.bundle_id == ip.bundle_id  # base auto-anchored + bundled
