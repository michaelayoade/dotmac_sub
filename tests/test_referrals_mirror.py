"""Read-only historical referral mirror compatibility."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from app.models.referral import ReferralMirror, ReferralProgramCache
from app.models.subscriber import Subscriber
from app.services import referrals_mirror


def _subscriber(db) -> Subscriber:
    subscriber = Subscriber(
        first_name="Legacy",
        last_name="Referrer",
        email=f"legacy-{uuid.uuid4().hex[:8]}@example.com",
    )
    db.add(subscriber)
    db.flush()
    return subscriber


def test_historical_mirror_read_is_local_and_shape_compatible(db_session):
    subscriber = _subscriber(db_session)
    db_session.add(
        ReferralProgramCache(
            subscriber_id=subscriber.id,
            code="LEGACY01",
            share_url="https://legacy.invalid/r/LEGACY01",
            program_enabled=True,
            reward_amount=Decimal("2500"),
            reward_currency="NGN",
            synced_at=datetime.now(UTC),
        )
    )
    db_session.add_all(
        (
            ReferralMirror(
                crm_referral_id="legacy-pending",
                subscriber_id=subscriber.id,
                referred_name="Historical prospect",
                status="pending",
            ),
            ReferralMirror(
                crm_referral_id="legacy-rewarded",
                subscriber_id=subscriber.id,
                referred_name="Historical customer",
                status="rewarded",
                reward_amount=Decimal("2500"),
                reward_currency="NGN",
                reward_status="paid",
            ),
        )
    )
    db_session.commit()

    result = referrals_mirror.read_for_subscriber(
        db_session,
        str(subscriber.id),
        refresh_ttl_seconds=0,
    )

    assert result["code"] == "LEGACY01"
    assert result["totals"] == {
        "total": 2,
        "pending": 1,
        "qualified": 0,
        "rewarded": 1,
        "total_earned": "2500.00",
    }
    assert {item["id"] for item in result["referrals"]} == {
        "legacy-pending",
        "legacy-rewarded",
    }


def test_missing_historical_cache_returns_empty_without_refresh(db_session):
    subscriber = _subscriber(db_session)
    db_session.commit()

    result = referrals_mirror.read_for_subscriber(db_session, str(subscriber.id))

    assert result["program"]["enabled"] is False
    assert result["totals"]["total"] == 0
    assert result["referrals"] == []


def test_historical_mirror_has_no_runtime_crm_paths():
    for retired in (
        "reconcile_subscriber",
        "reconcile_all",
        "apply_webhook",
        "refer_a_friend",
    ):
        assert not hasattr(referrals_mirror, retired)
