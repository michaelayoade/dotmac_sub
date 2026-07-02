from __future__ import annotations

from sqlalchemy import select

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


def suspend_bundle(db, bundle_id, reason, source):
    """Suspend every member of the bundle atomically. Returns members affected."""
    from app.services.account_lifecycle import suspend_subscription

    count = 0
    for sub in bundle_members(db, bundle_id):
        try:
            suspend_subscription(db, str(sub.id), reason=reason, source=source)
            count += 1
        except ValueError as exc:
            if "Cannot suspend" not in str(exc):
                raise
    return count


def restore_bundle(db, bundle_id, trigger, resolved_by):
    """Restore every member of the bundle. Returns members affected."""
    from app.services.account_lifecycle import restore_subscription

    count = 0
    for sub in bundle_members(db, bundle_id):
        try:
            restore_subscription(db, str(sub.id), trigger=trigger, resolved_by=resolved_by)
            count += 1
        except ValueError:
            pass
    return count


def expire_bundle(db, bundle_id, **kwargs):
    """Expire every member of the bundle. Returns members affected."""
    from app.services.account_lifecycle import expire_subscription

    count = 0
    for sub in bundle_members(db, bundle_id):
        try:
            expire_subscription(db, str(sub.id), **kwargs)
            count += 1
        except ValueError:
            pass
    return count


def reconcile_bundle_states(db, bundle_id=None):
    """Converge non-anchor members to the anchor's enforcement state.

    A bundle member whose suspended/active state disagrees with its anchor is a
    divergence (e.g. base active while the /29 sub is suspended). This restores
    or suspends the straggler to match the anchor, making partial states
    self-healing. Returns ``{"bundles_scanned", "members_converged"}``.
    """
    from app.models.catalog import SubscriptionStatus
    from app.models.enforcement_lock import EnforcementReason
    from app.services.account_lifecycle import (
        restore_subscription,
        suspend_subscription,
    )

    q = select(SubscriptionBundle).where(SubscriptionBundle.is_active.is_(True))
    if bundle_id is not None:
        q = q.where(SubscriptionBundle.id == coerce_uuid(bundle_id))
    scanned = converged = 0
    for bundle in db.scalars(q).all():
        scanned += 1
        anchor = (
            db.get(Subscription, bundle.anchor_subscription_id)
            if bundle.anchor_subscription_id
            else None
        )
        if anchor is None:
            continue
        target_suspended = anchor.status == SubscriptionStatus.suspended
        for sub in bundle_members(db, str(bundle.id)):
            if sub.id == anchor.id:
                continue
            if (sub.status == SubscriptionStatus.suspended) == target_suspended:
                continue
            try:
                if target_suspended:
                    suspend_subscription(
                        db, str(sub.id),
                        reason=EnforcementReason.overdue, source="bundle_reconcile",
                    )
                else:
                    # The bundle is current (anchor active), so a straggler
                    # member's overdue lock is resolved like any collections
                    # resolution — an ALLOWED_RESTORERS-authorized trigger.
                    restore_subscription(
                        db, str(sub.id),
                        trigger="collections_resolution",
                        resolved_by=f"bundle_reconcile:{bundle.id}",
                    )
                converged += 1
            except ValueError:
                pass
    return {"bundles_scanned": scanned, "members_converged": converged}


def _find_base_internet_sub(db, subscriber_id):
    """The subscriber's base (non-IP) internet subscription, or None."""
    from app.models.catalog import SubscriptionStatus

    subs = db.scalars(
        select(Subscription).where(
            Subscription.subscriber_id == coerce_uuid(subscriber_id),
            Subscription.status.in_(
                [SubscriptionStatus.active, SubscriptionStatus.suspended]
            ),
        )
    ).all()
    for sub in subs:
        d = (sub.service_description or "").lower()
        if "slash" not in d and "pppoe public ip" not in d:
            return sub
    return None


def attach_component(db, subscriber_id, subscription_id):
    """Attach a new component subscription (IP, voice, …) to the subscriber's bundle.

    Uses the subscriber's active bundle, or creates one anchored on their base
    internet subscription. No-op if the component is already bundled, or if the
    subscriber has no base internet service to anchor on (left unbundled).
    """
    sub = db.get(Subscription, coerce_uuid(subscription_id))
    if sub is None:
        raise ValueError(f"subscription {subscription_id} not found")
    if sub.bundle_id is not None:
        return
    bundle = db.scalar(
        select(SubscriptionBundle)
        .where(
            SubscriptionBundle.subscriber_id == coerce_uuid(subscriber_id),
            SubscriptionBundle.is_active.is_(True),
        )
        .limit(1)
    )
    if bundle is None:
        anchor = _find_base_internet_sub(db, subscriber_id)
        if anchor is None:
            return
        bundle = create_bundle(db, str(subscriber_id), str(anchor.id))
        add_member(db, str(bundle.id), str(anchor.id))
    add_member(db, str(bundle.id), str(subscription_id))
