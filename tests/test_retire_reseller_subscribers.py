"""Layer 3 Phase 4: retire orphaned reseller Subscriber rows after cutover."""

from __future__ import annotations

from app.models.auth import AuthProvider, UserCredential
from app.models.subscriber import Reseller, Subscriber, SubscriberStatus, UserType
from app.services.auth_flow import hash_password
from scripts.one_off import retire_migrated_reseller_subscribers as rt


def _reseller_sub(db, email, *, with_credential):
    r = Reseller(name=f"R-{email}", code=email[:6])
    db.add(r)
    db.flush()
    s = Subscriber(
        first_name="R",
        last_name="S",
        email=email,
        user_type=UserType.reseller,
        reseller_id=r.id,
        status=SubscriberStatus.active,
        is_active=True,
    )
    db.add(s)
    db.flush()
    if with_credential:
        db.add(
            UserCredential(
                subscriber_id=s.id,
                provider=AuthProvider.local,
                username=email,
                password_hash=hash_password("secret"),
                is_active=True,  # noqa: S106
            )
        )
    db.commit()
    return s


def test_retires_only_migrated_reseller_subscribers(db_session):
    migrated = _reseller_sub(db_session, "migrated@r.example", with_credential=False)
    still_live = _reseller_sub(db_session, "live@r.example", with_credential=True)

    rt._apply(db_session)
    db_session.refresh(migrated)
    db_session.refresh(still_live)
    assert migrated.status == SubscriberStatus.canceled
    assert migrated.is_active is False
    # A reseller subscriber that still has an active login is NOT retired.
    assert still_live.status == SubscriberStatus.active


def test_retire_is_reversible(db_session):
    migrated = _reseller_sub(db_session, "rev@r.example", with_credential=False)
    rt._apply(db_session)
    db_session.refresh(migrated)
    assert migrated.status == SubscriberStatus.canceled
    rt._rollback(db_session)
    db_session.refresh(migrated)
    assert migrated.status == SubscriberStatus.active
    assert migrated.is_active is True
