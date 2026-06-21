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


def test_throttle_lifted_restores_original_profile(
    db_session, subscriber, catalog_offer
):
    full = RadiusProfile(name="full-speed", is_active=True)
    throttle = RadiusProfile(name="throttle", is_active=True)
    db_session.add_all([full, throttle])
    db_session.flush()

    sub = _sub(db_session, subscriber, catalog_offer)
    cred = AccessCredential(
        subscriber_id=subscriber.id,
        username="fup-user",
        is_active=True,
        radius_profile_id=throttle.id,  # currently throttled on the wire
    )
    db_session.add(cred)
    db_session.add(
        FupState(
            subscription_id=sub.id,
            offer_id=catalog_offer.id,
            action_status=FupActionStatus.throttled,
            throttle_profile_id=throttle.id,
            original_profile_id=full.id,
            cap_resets_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )
    db_session.commit()

    result = lift_fup_enforcement(db_session, str(sub.id))

    assert "restore_profile" in result["actions"]
    db_session.refresh(cred)
    assert cred.radius_profile_id == full.id

    state = db_session.query(FupState).filter_by(subscription_id=sub.id).one()
    assert state.action_status == FupActionStatus.none


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

    result = lift_fup_enforcement(db_session, str(sub.id))

    assert "resume" in result["actions"]
    db_session.refresh(sub)
    assert sub.status == SubscriptionStatus.active
    assert not get_active_locks(db_session, subscription_id=str(sub.id))

    state = db_session.query(FupState).filter_by(subscription_id=sub.id).one()
    assert state.action_status == FupActionStatus.none


def test_lift_noop_when_no_state(db_session, subscriber, catalog_offer):
    sub = _sub(db_session, subscriber, catalog_offer)
    result = lift_fup_enforcement(db_session, str(sub.id))
    assert result["lifted"] is False
