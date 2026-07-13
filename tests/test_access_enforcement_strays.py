"""Enforcement must be undone as precisely as it was applied.

Three strays from the 2026-07-13 re-audit, all of which left a paying customer
worse off than the system believed:

S1  A dunning throttle was never lifted when the customer paid. The pre-throttle
    profile existed only in a log line, so their real speed was unrecoverable —
    and radius_population re-applied the throttle on every sweep.
S2  Suspending ONE subscription hard-deleted RADIUS rows for the WHOLE subscriber,
    taking their other paid services offline.
S3  The reseller portal suspended with emit=False (so nothing enforced it) and
    reactivated with a raw status write (so nothing re-provisioned it).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from app.models.catalog import (
    AccessCredential,
    AccessType,
    BillingMode,
    CatalogOffer,
    PriceBasis,
    RadiusProfile,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.subscriber import Subscriber
from app.services.collections._core import _restore_throttle, _throttle_account


def _profile(db, name: str) -> RadiusProfile:
    profile = RadiusProfile(name=name, is_active=True)
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def _offer(db) -> CatalogOffer:
    offer = CatalogOffer(
        name=f"Offer {uuid.uuid4().hex[:6]}",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_mode=BillingMode.prepaid,
        billing_cycle="monthly",
    )
    db.add(offer)
    db.commit()
    db.refresh(offer)
    return offer


def _account(db) -> Subscriber:
    sub = Subscriber(
        first_name="T",
        last_name="User",
        email=f"t{uuid.uuid4().hex[:8]}@example.com",
        status="active",
        is_active=True,
        billing_mode=BillingMode.prepaid,
        min_balance=Decimal("0.00"),
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def _subscription(db, account, offer, status=SubscriptionStatus.active) -> Subscription:
    sub = Subscription(
        subscriber_id=account.id,
        offer_id=offer.id,
        status=status,
        billing_mode=BillingMode.prepaid,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def _credential(db, account, subscription, profile) -> AccessCredential:
    cred = AccessCredential(
        subscriber_id=account.id,
        subscription_id=subscription.id if subscription else None,
        username=f"u{uuid.uuid4().hex[:8]}",
        is_active=True,
        radius_profile_id=profile.id if profile else None,
    )
    db.add(cred)
    db.commit()
    db.refresh(cred)
    return cred


# --- S1: the throttle must be undone exactly ---------------------------------


def test_throttle_persists_the_profile_it_replaces(db_session, monkeypatch):
    """The customer's real speed must survive the throttle."""
    account = _account(db_session)
    offer = _offer(db_session)
    subscription = _subscription(db_session, account, offer)
    real = _profile(db_session, "Gold 100Mbps")
    throttle = _profile(db_session, "Collections Throttle")
    cred = _credential(db_session, account, subscription, real)

    monkeypatch.setattr(
        "app.services.collections._core.settings_spec.resolve_value",
        lambda db, domain, key: str(throttle.id),
    )

    ok, count = _throttle_account(db_session, str(account.id))
    db_session.commit()
    db_session.refresh(cred)

    assert ok and count == 1
    assert cred.radius_profile_id == throttle.id
    assert cred.pre_throttle_radius_profile_id == real.id, (
        "the profile the throttle replaced was not persisted — the customer's "
        "real speed is unrecoverable once they pay"
    )


def test_paying_restores_the_exact_profile_that_was_throttled(db_session, monkeypatch):
    account = _account(db_session)
    offer = _offer(db_session)
    subscription = _subscription(db_session, account, offer)
    real = _profile(db_session, "Gold 100Mbps")
    throttle = _profile(db_session, "Collections Throttle")
    cred = _credential(db_session, account, subscription, real)

    monkeypatch.setattr(
        "app.services.collections._core.settings_spec.resolve_value",
        lambda db, domain, key: str(throttle.id),
    )

    _throttle_account(db_session, str(account.id))
    db_session.commit()

    restored = _restore_throttle(db_session, str(account.id))
    db_session.commit()
    db_session.refresh(cred)

    assert restored == 1
    assert cred.radius_profile_id == real.id, (
        "the customer paid and did not get their speed back"
    )
    assert cred.pre_throttle_radius_profile_id is None


def test_rethrottling_does_not_overwrite_the_real_profile(db_session, monkeypatch):
    """A second throttle pass must not record the throttle AS the real profile."""
    account = _account(db_session)
    offer = _offer(db_session)
    subscription = _subscription(db_session, account, offer)
    real = _profile(db_session, "Gold 100Mbps")
    throttle = _profile(db_session, "Collections Throttle")
    cred = _credential(db_session, account, subscription, real)

    monkeypatch.setattr(
        "app.services.collections._core.settings_spec.resolve_value",
        lambda db, domain, key: str(throttle.id),
    )

    _throttle_account(db_session, str(account.id))
    db_session.commit()
    _throttle_account(db_session, str(account.id))  # sweep runs again
    db_session.commit()
    db_session.refresh(cred)

    assert cred.pre_throttle_radius_profile_id == real.id, (
        "re-throttling overwrote the remembered profile with the throttle profile"
    )


# --- S2: suspension must not take out the customer's other services ----------


def test_suspending_one_subscription_spares_a_sibling_service(db_session, monkeypatch):
    """The whole-subscriber RADIUS wipe (F10)."""
    from app.services import enforcement as enforcement_service

    account = _account(db_session)
    offer = _offer(db_session)
    home = _subscription(db_session, account, offer)
    office = _subscription(db_session, account, offer)  # still paid, still active
    profile = _profile(db_session, "Standard")
    home_cred = _credential(db_session, account, home, profile)
    office_cred = _credential(db_session, account, office, profile)

    removed: list[str] = []

    def _spy(db, credentials):
        removed.extend(str(c.id) for c in credentials)

    monkeypatch.setattr(
        enforcement_service, "_remove_credentials_from_external_radius", _spy
    )

    enforcement_service.cleanup_subscription_on_suspend(db_session, str(home.id))

    assert str(home_cred.id) in removed
    assert str(office_cred.id) not in removed, (
        "suspending one subscription deleted the RADIUS rows for the customer's "
        "other, still-paid service"
    )


def test_suspension_still_removes_an_unlinked_credential_when_its_the_only_service(
    db_session, monkeypatch
):
    """Legacy credentials carry a NULL subscription_id and must still be removed.

    Otherwise the S2 fix would silently stop suspension working for exactly the
    customers who have them.
    """
    from app.services import enforcement as enforcement_service

    account = _account(db_session)
    offer = _offer(db_session)
    only = _subscription(db_session, account, offer)
    profile = _profile(db_session, "Standard")
    legacy_cred = _credential(
        db_session, account, None, profile
    )  # subscription_id NULL

    removed: list[str] = []
    monkeypatch.setattr(
        enforcement_service,
        "_remove_credentials_from_external_radius",
        lambda db, credentials: removed.extend(str(c.id) for c in credentials),
    )

    enforcement_service.cleanup_subscription_on_suspend(db_session, str(only.id))

    assert str(legacy_cred.id) in removed, (
        "an unlinked legacy credential was left in RADIUS, so the suspension "
        "never took effect"
    )


# --- S3: a lock-less suspended subscription is restorable by the owner --------


def test_restore_subscription_restores_a_lockless_suspended_subscription(db_session):
    """The owner decides this once, so no caller has to raw-write status.

    The reseller portal used to hand-write ``status = active`` here, which emitted
    no event — the IP was never re-provisioned and RADIUS never re-synced, so the
    UI said active while the customer stayed offline.
    """
    from app.services.account_lifecycle import restore_subscription

    account = _account(db_session)
    offer = _offer(db_session)
    subscription = _subscription(
        db_session, account, offer, status=SubscriptionStatus.suspended
    )

    restored = restore_subscription(
        db_session,
        str(subscription.id),
        trigger="admin",
        resolved_by="admin",
    )
    db_session.commit()
    db_session.refresh(subscription)

    assert restored is True
    assert subscription.status == SubscriptionStatus.active


def test_restore_subscription_still_refuses_when_a_lock_it_cannot_clear_remains(
    db_session,
):
    """Refusing an unauthorized trigger is correct and must not regress.

    A payment must never lift a fraud block.
    """
    from app.models.enforcement_lock import EnforcementReason
    from app.services.account_lifecycle import (
        restore_subscription,
        suspend_subscription,
    )

    account = _account(db_session)
    offer = _offer(db_session)
    subscription = _subscription(db_session, account, offer)

    suspend_subscription(
        db_session,
        str(subscription.id),
        reason=EnforcementReason.fraud,
        source="test",
        emit=False,
    )
    db_session.commit()

    restored = restore_subscription(
        db_session,
        str(subscription.id),
        trigger="payment",  # not allowed to clear a fraud lock
        resolved_by="payment",
    )
    db_session.commit()
    db_session.refresh(subscription)

    assert restored is False
    assert subscription.status != SubscriptionStatus.active
