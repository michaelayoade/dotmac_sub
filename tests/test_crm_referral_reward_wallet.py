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


def test_credit_endpoint_requires_external_ref(db_session):
    """Without external_ref the wallet entry reference is NULL (unconstrained),
    so a retry would double-credit. The endpoint must reject it."""
    from fastapi import HTTPException

    from app.api.crm import create_crm_credit

    sub = _subscriber(db_session)
    with pytest.raises(HTTPException) as exc:
        create_crm_credit(
            payload={"subscriber_id": str(sub.id), "amount": "5000"},
            db=db_session,
        )
    assert exc.value.status_code == 400
    assert "external_ref" in str(exc.value.detail)


def test_credit_integrity_error_reraised_when_no_existing_row(db_session, monkeypatch):
    """If credit_wallet raises IntegrityError but no matching entry can be found
    on re-query, the error is not swallowed (only a genuine duplicate is)."""
    from sqlalchemy.exc import IntegrityError

    from app.services import vas_wallet

    sub = _subscriber(db_session)

    def boom(db, wallet, **kw):
        raise IntegrityError("some other constraint", {}, Exception())

    monkeypatch.setattr(vas_wallet, "credit_wallet", boom)

    with pytest.raises(IntegrityError):
        crm_api.credit_referral_reward_to_wallet(
            db_session,
            subscriber_id=str(sub.id),
            amount=Decimal("5000"),
            external_ref="referral:no-existing",
        )
