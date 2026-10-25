"""FUP enforcement lift on period reset (review tasks #10, #11).

The period-boundary reset must undo the actual enforcement, not just null the
FupState row — otherwise a subscriber stays throttled/blocked forever.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.models.catalog import (
    AccessCredential,
    RadiusProfile,
    Subscription,
    SubscriptionStatus,
)
from app.models.enforcement_lock import EnforcementReason
from app.models.fup_state import FupActionStatus, FupState
from app.services.account_lifecycle import get_active_locks, suspend_subscription
from app.services.enforcement import lift_fup_enforcement

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def _sub(db, subscriber, catalog_offer, status=SubscriptionStatus.active):
    sub = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=status,
        billing_mode=catalog_offer.billing_mode,
    )
    db.add(sub)
    db.flush()
    return sub


def _throttled_service(db, subscriber, catalog_offer, *, slot, marker=None, throttle):
    full = RadiusProfile(name=f"full-{slot is None}", is_active=True)
    db.add(full)
    db.flush()
    sub = _sub(db, subscriber, catalog_offer)
    cred = AccessCredential(
        subscriber_id=subscriber.id,
        username="fup-user",
        is_active=True,
        radius_profile_id=slot.id if slot else None,
        pre_throttle_radius_profile_id=marker.id if marker else None,
    )
    db.add(cred)
    db.add(
        FupState(
            subscription_id=sub.id,
            offer_id=catalog_offer.id,
            action_status=FupActionStatus.throttled,
            throttle_profile_id=throttle.id,
            original_profile_id=full.id,
            cap_resets_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )
    db.commit()
    return sub, cred


def test_throttle_lift_clears_the_throttle_an_older_version_left_in_the_slot(
    db_session, subscriber, catalog_offer, monkeypatch
):
    monkeypatch.setattr(
        "app.services.radius_population.reconcile_usernames",
        lambda *_args, **_kwargs: {"projection_targets": 1},
    )
    throttle = RadiusProfile(name="throttle", is_active=True)
    db_session.add(throttle)
    db_session.flush()
    sub, cred = _throttled_service(
        db_session, subscriber, catalog_offer, slot=throttle, throttle=throttle
    )

    result = lift_fup_enforcement(db_session, str(sub.id), evaluated_at=NOW)

    assert "restore_profile" in result["actions"]
    db_session.refresh(cred)
    assert cred.radius_profile_id is None  # the rate owner derives the plan again
    state = db_session.query(FupState).filter_by(subscription_id=sub.id).one()
    assert state.action_status == FupActionStatus.none


def test_throttle_lift_projects_after_the_state_is_cleared(
    db_session, subscriber, catalog_offer, monkeypatch
):
    seen = []

    def project(usernames, **kwargs):
        state = (
            kwargs["source_db"]
            .query(FupState)
            .filter(FupState.action_status == FupActionStatus.throttled)
            .count()
        )
        seen.append(state)
        return {"projection_targets": 1}

    monkeypatch.setattr("app.services.radius_population.reconcile_usernames", project)
    throttle = RadiusProfile(name="throttle", is_active=True)
    db_session.add(throttle)
    db_session.flush()
    sub, _cred = _throttled_service(
        db_session, subscriber, catalog_offer, slot=None, throttle=throttle
    )

    lift_fup_enforcement(db_session, str(sub.id), evaluated_at=NOW)

    assert seen == [0], "the projection must not still see the FUP throttle"


def test_fup_lift_keeps_an_active_collections_throttle(
    db_session, subscriber, catalog_offer, monkeypatch
):
    """Lifting the FUP must not wipe a collections throttle on the same service."""
    from app.models.domain_settings import SettingDomain, SettingValueType
    from app.schemas.settings import DomainSettingUpdate
    from app.services import access_rate_profile as owner
    from app.services.domain_settings import DomainSettings

    monkeypatch.setattr(
        "app.services.radius_population.reconcile_usernames",
        lambda *_args, **_kwargs: {"projection_targets": 1},
    )
    fup = RadiusProfile(name="fup", is_active=True)
    collections = RadiusProfile(name="collections", is_active=True, download_speed=500)
    plan = RadiusProfile(name="plan", is_active=True)
    db_session.add_all([fup, collections, plan])
    db_session.flush()
    DomainSettings(SettingDomain.collections).upsert_by_key(
        db_session,
        "throttle_radius_profile_id",
        DomainSettingUpdate(
            value_type=SettingValueType.string, value_text=str(collections.id)
        ),
    )
    sub, cred = _throttled_service(
        db_session,
        subscriber,
        catalog_offer,
        slot=collections,
        marker=plan,
        throttle=fup,
    )

    lift_fup_enforcement(db_session, str(sub.id), evaluated_at=NOW)

    db_session.refresh(cred)
    assert cred.radius_profile_id == collections.id
    assert cred.pre_throttle_radius_profile_id == plan.id
    decision = owner.resolve_effective_rate(db_session, sub, cred)
    assert decision.layer is owner.RateLayer.collections_throttle


def test_fup_lift_keeps_a_slot_that_is_the_collections_profile(
    db_session, subscriber, catalog_offer, monkeypatch
):
    """The FUP fell back to the same profile collections uses: leave it."""
    from app.models.domain_settings import SettingDomain, SettingValueType
    from app.schemas.settings import DomainSettingUpdate
    from app.services.domain_settings import DomainSettings

    monkeypatch.setattr(
        "app.services.radius_population.reconcile_usernames",
        lambda *_args, **_kwargs: {"projection_targets": 1},
    )
    shared = RadiusProfile(name="shared", is_active=True)
    db_session.add(shared)
    db_session.flush()
    DomainSettings(SettingDomain.collections).upsert_by_key(
        db_session,
        "throttle_radius_profile_id",
        DomainSettingUpdate(
            value_type=SettingValueType.string, value_text=str(shared.id)
        ),
    )
    sub, cred = _throttled_service(
        db_session, subscriber, catalog_offer, slot=shared, throttle=shared
    )

    lift_fup_enforcement(db_session, str(sub.id), evaluated_at=NOW)

    db_session.refresh(cred)
    assert cred.radius_profile_id == shared.id


def test_blocked_via_suspension_lifted_resumes_subscription(
    db_session, subscriber, catalog_offer
):
    sub = _sub(db_session, subscriber, catalog_offer)
    # Simulate FUP suspend: lock + suspended status + blocked FUP state.
    suspend_subscription(
        db_session,
        str(sub.id),
        reason=EnforcementReason.fup,
        source="fup_exhausted",
        emit=False,
    )
    db_session.add(
        FupState(
            subscription_id=sub.id,
            offer_id=catalog_offer.id,
            action_status=FupActionStatus.blocked,
            cap_resets_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )
    db_session.commit()
    assert sub.status == SubscriptionStatus.suspended

    result = lift_fup_enforcement(db_session, str(sub.id), evaluated_at=NOW)

    assert "resume" in result["actions"]
    db_session.refresh(sub)
    assert sub.status == SubscriptionStatus.active
    assert not get_active_locks(db_session, subscription_id=str(sub.id))

    state = db_session.query(FupState).filter_by(subscription_id=sub.id).one()
    assert state.action_status == FupActionStatus.none


def test_lift_noop_when_no_state(db_session, subscriber, catalog_offer):
    sub = _sub(db_session, subscriber, catalog_offer)
    result = lift_fup_enforcement(db_session, str(sub.id), evaluated_at=NOW)
    assert result["lifted"] is False
