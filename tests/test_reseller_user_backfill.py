"""Layer 3 Phase 2 backfill: reseller subscriber login -> ResellerUser principal."""

from __future__ import annotations

from app.models.auth import AuthProvider, MFAMethod, MFAMethodType, UserCredential
from app.models.subscriber import Reseller, ResellerUser, Subscriber, UserType
from app.services.auth_flow import hash_password
from scripts.one_off import backfill_reseller_user_principals as bf


def _reseller_login(db, *, code="BF", email="boss@bf.example"):
    r = Reseller(name=f"BF {code}", code=code)
    db.add(r)
    db.flush()
    sub = Subscriber(
        first_name="Boss",
        last_name="Person",
        email=email,
        user_type=UserType.reseller,
        reseller_id=r.id,
    )
    db.add(sub)
    db.flush()
    db.add(ResellerUser(subscriber_id=sub.id, reseller_id=r.id, is_active=True))
    cred = UserCredential(
        subscriber_id=sub.id,
        provider=AuthProvider.local,
        username="boss-login",
        password_hash=hash_password("secret"),  # noqa: S106
        is_active=True,
    )
    db.add(cred)
    db.add(
        MFAMethod(
            subscriber_id=sub.id,
            method_type=MFAMethodType.totp,
            is_primary=True,
            enabled=True,
            is_active=True,
        )
    )
    db.commit()
    return r, sub, cred


def test_apply_repoints_credential_and_mfa_to_reseller_user(db_session):
    r, sub, cred = _reseller_login(db_session)
    bf._apply(db_session)

    db_session.refresh(cred)
    assert cred.subscriber_id is None
    assert cred.reseller_user_id is not None

    ru = db_session.get(ResellerUser, cred.reseller_user_id)
    assert ru.subscriber_id == sub.id  # linkage retained for rollback
    assert ru.reseller_id == r.id
    assert ru.email == sub.email
    assert ru.full_name  # populated from the subscriber

    mfa = db_session.query(MFAMethod).filter(MFAMethod.reseller_user_id == ru.id).one()
    assert mfa.subscriber_id is None


def test_apply_is_idempotent(db_session):
    _r, _sub, cred = _reseller_login(db_session, code="BF2", email="b2@bf.example")
    bf._apply(db_session)
    first = db_session.get(UserCredential, cred.id).reseller_user_id
    bf._apply(db_session)  # second run: nothing left on subscriber side
    db_session.refresh(cred)
    assert cred.reseller_user_id == first


def test_rollback_restores_subscriber_principal(db_session):
    _r, sub, cred = _reseller_login(db_session, code="BF3", email="b3@bf.example")
    bf._apply(db_session)
    bf._rollback(db_session)

    db_session.refresh(cred)
    assert cred.reseller_user_id is None
    assert cred.subscriber_id == sub.id
    mfa = db_session.query(MFAMethod).filter(MFAMethod.subscriber_id == sub.id).one()
    assert mfa.reseller_user_id is None
