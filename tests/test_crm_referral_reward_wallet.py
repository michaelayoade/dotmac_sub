"""Referral reward pays into the VAS wallet (individual subscribers), idempotently."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.models.subscriber import Subscriber
from app.services import crm_api, vas_wallet


def _subscriber(db):
    sub = Subscriber(
        first_name="Cust",
        last_name="Omer",
        email=f"c-{uuid.uuid4().hex[:8]}@example.com",
        is_active=True,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def test_reward_credits_wallet_and_is_idempotent(db_session):
    sub = _subscriber(db_session)
    e1 = crm_api.credit_referral_reward_to_wallet(
        db_session,
        subscriber_id=str(sub.id),
        amount=Decimal("5000"),
        reason="Referral reward",
        external_ref="referral:r1",
    )
    e2 = crm_api.credit_referral_reward_to_wallet(
        db_session,
        subscriber_id=str(sub.id),
        amount=Decimal("5000"),
        external_ref="referral:r1",
    )
    assert e1.id == e2.id  # idempotent on external_ref → no double pay

    wallet = vas_wallet.get_or_create_wallet(db_session, str(sub.id))
    assert vas_wallet.wallet_balance(db_session, wallet.id) == Decimal("5000")


def test_reward_unknown_subscriber_raises_lookup(db_session):
    with pytest.raises(LookupError):
        crm_api.credit_referral_reward_to_wallet(
            db_session,
            subscriber_id=str(uuid.uuid4()),
            amount=Decimal("100"),
            external_ref="referral:x",
        )
