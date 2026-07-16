"""Referrals module flow on PostgreSQL: refer → qualify → reward, all on the
native path (referrals_native_write_enabled ON).

Nothing external is faked: the reward credit is a real issued CreditNote via
``crm_api.create_account_credit`` with the shared ``referral:{id}``
idempotency namespace, on a real database (the row lock in ``issue_reward``
is PG-only surface the unit suite can't exercise).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.models.billing import CreditNote
from app.models.domain_settings import DomainSetting, SettingDomain, SettingValueType
from app.models.referral_native import Referral
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services.referrals import referrals
from app.services.subscriber import _default_reseller_id


def _program(db, *, amount: str = "2500") -> None:
    for key, text, vtype in (
        ("referral_program_enabled", "true", SettingValueType.boolean),
        ("referral_reward_amount", amount, SettingValueType.string),
    ):
        db.add(
            DomainSetting(
                domain=SettingDomain.subscriber,
                key=key,
                value_type=vtype,
                value_text=text,
                is_active=True,
            )
        )
    db.flush()


def _subscriber(db) -> Subscriber:
    sub = Subscriber(
        first_name="Flow",
        last_name="Referral",
        email=f"fr-{uuid.uuid4().hex[:8]}@example.com",
        # subscribers.reseller_id is NOT NULL (migration 116); default to House.
        reseller_id=_default_reseller_id(db),
    )
    db.add(sub)
    db.flush()
    return sub


@pytest.fixture(autouse=True)
def _native_write(enable_flags, db_session):
    enable_flags("referrals_native_write_enabled")
    _program(db_session)


def test_referral_lifecycle_native(db_session):
    referrer = _subscriber(db_session)
    friend_email = f"friend-{uuid.uuid4().hex[:8]}@example.com"

    # 1. Refer — native, referrer has NO CRM link.
    out = referrals.refer_a_friend(
        db_session,
        str(referrer.id),
        name="Flow Friend",
        email=friend_email,
        phone="08031112222",
    )
    assert out["status"] == "pending"
    referral = db_session.get(Referral, uuid.UUID(out["id"]))
    assert referral is not None
    assert referral.referrer_subscriber_id == referrer.id

    # 2. Qualify — the referred prospect becomes an active subscriber.
    prospect = db_session.get(Subscriber, referral.referred_subscriber_id)
    prospect.status = SubscriberStatus.active
    db_session.flush()
    qualified = referrals.qualify_for_subscriber(db_session, prospect)
    assert qualified is not None and qualified.id == referral.id
    assert qualified.status == "qualified"
    assert qualified.reward_amount == Decimal("2500")

    # 3. Reward — a real issued CreditNote lands on the referrer's account,
    # memo-marked with the shared idempotency namespace.
    rewarded = referrals.issue_reward(db_session, str(referral.id))
    assert rewarded.status == "rewarded"
    assert rewarded.reward_status == "issued"
    marker = f"[ref:referral:{referral.id}]"
    notes = (
        db_session.query(CreditNote)
        .filter(CreditNote.account_id == referrer.id)
        .filter(CreditNote.memo.ilike(f"%{marker}%"))
        .all()
    )
    assert len(notes) == 1
    assert notes[0].total == Decimal("2500")
    assert (rewarded.metadata_ or {}).get("reward_credit_id") == str(notes[0].id)

    # 4. Idempotency: issuing again returns the same credit, no re-credit —
    # the same guarantee that makes a pre-cutover CRM payout safe to replay.
    again = referrals.issue_reward(db_session, str(referral.id))
    assert again.reward_status == "issued"
    assert (again.metadata_ or {}).get("reward_credit_id") == str(notes[0].id)
    assert (
        db_session.query(CreditNote)
        .filter(CreditNote.account_id == referrer.id)
        .filter(CreditNote.memo.ilike(f"%{marker}%"))
        .count()
        == 1
    )


def test_double_capture_guard_native(db_session):
    """Referring the same contact twice yields one active referral row."""
    referrer = _subscriber(db_session)
    friend_email = f"dup-{uuid.uuid4().hex[:8]}@example.com"
    first = referrals.refer_a_friend(
        db_session, str(referrer.id), name="Dup Friend", email=friend_email
    )
    second = referrals.refer_a_friend(
        db_session, str(referrer.id), name="Dup Friend", email=friend_email
    )
    assert first["id"] == second["id"]
    assert (
        db_session.query(Referral)
        .filter(Referral.referrer_subscriber_id == referrer.id)
        .filter(Referral.is_active.is_(True))
        .count()
        == 1
    )
