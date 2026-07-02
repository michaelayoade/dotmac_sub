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
