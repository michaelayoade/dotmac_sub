from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import CatalogOffer, Subscription, SubscriptionBundle
from app.services.common import coerce_uuid


def create_bundle(db, subscriber_id, anchor_subscription_id, label=None):
    bundle = SubscriptionBundle(
        subscriber_id=coerce_uuid(subscriber_id),
        anchor_subscription_id=coerce_uuid(anchor_subscription_id),
        label=label,
        status="active",
    )
    db.add(bundle)
    db.flush()
    return bundle


def add_member(db, bundle_id, subscription_id):
    sub = db.get(Subscription, coerce_uuid(subscription_id))
    if sub is None:
        raise ValueError(f"subscription {subscription_id} not found")
    sub.bundle_id = coerce_uuid(bundle_id)
    db.flush()
    recompute_is_dedicated(db, bundle_id)


def bundle_members(db, bundle_id):
    return list(
        db.scalars(
            select(Subscription).where(Subscription.bundle_id == coerce_uuid(bundle_id))
        ).all()
    )


def recompute_is_dedicated(db, bundle_id):
    bundle = db.get(SubscriptionBundle, coerce_uuid(bundle_id))
    if bundle is None:
        return False
    dedicated = db.scalar(
        select(CatalogOffer.plan_family)
        .join(Subscription, Subscription.offer_id == CatalogOffer.id)
        .where(Subscription.bundle_id == bundle.id, CatalogOffer.plan_family == "dedicated")
        .limit(1)
    )
    bundle.is_dedicated = dedicated is not None
    db.flush()
    return bundle.is_dedicated
