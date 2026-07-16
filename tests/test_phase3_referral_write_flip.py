"""Phase 3 §4.3 — refer-a-friend write surfaces behind ``referrals_native_write_enabled``.

OFF (default) keeps the CRM write-through via ``referrals_mirror.refer_a_friend``
(which 409s for subscribers without a CRM link); ON captures the referral in
sub's native tables via ``Referrals.refer_a_friend`` — no CRM link required.
Reward money is unaffected by the flip: ``financial.credit_notes`` owns
crediting behind the shared ``referral:{id}`` idempotency namespace.
"""

from __future__ import annotations

import uuid

from app.api import me as me_api
from app.models.referral_native import Referral
from app.models.subscriber import Subscriber
from app.schemas.portal import ReferAFriendRequest
from app.services import referrals as referrals_service


def _subscriber(db) -> Subscriber:
    sub = Subscriber(
        first_name="C",
        last_name="R",
        email=f"c-{uuid.uuid4().hex[:8]}@example.com",
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def test_native_write_flag_defaults_off(db_session):
    # Spec default False — the CRM write-through stays the live path until
    # the coordinated §4.3 write flip.
    assert referrals_service.native_write_enabled(db_session) is False


def test_me_referral_flag_off_writes_through_mirror(db_session, monkeypatch):
    sub = _subscriber(db_session)
    principal = {"principal_type": "subscriber", "subscriber_id": str(sub.id)}
    monkeypatch.setattr(referrals_service, "native_write_enabled", lambda db: False)
    captured = {}

    def _mirror(db, sid, **kw):
        captured["sid"] = sid
        return {"id": "r-crm-1", "status": "pending", "message": "ok"}

    monkeypatch.setattr(me_api.referrals_mirror, "refer_a_friend", _mirror)
    out = me_api.my_refer_a_friend(
        ReferAFriendRequest(name="Ada", phone="0803"),
        db=db_session,
        principal=principal,
    )
    assert out["id"] == "r-crm-1"
    assert captured["sid"] == str(sub.id)


def test_me_referral_flag_on_captures_natively(db_session, monkeypatch):
    """Flag ON: the referral lands in sub's native table, keyed to the
    referrer's code, with the mirror-compatible response shape."""
    sub = _subscriber(db_session)
    principal = {"principal_type": "subscriber", "subscriber_id": str(sub.id)}
    monkeypatch.setattr(referrals_service, "native_write_enabled", lambda db: True)
    out = me_api.my_refer_a_friend(
        ReferAFriendRequest(name="Ada Friend", phone="08031234567"),
        db=db_session,
        principal=principal,
    )
    assert set(out) >= {"id", "status", "message"}
    row = db_session.get(Referral, uuid.UUID(str(out["id"])))
    assert row is not None
    assert row.status == "pending"


def test_me_referral_native_needs_no_crm_link(db_session, monkeypatch):
    """Rewards regression: a native-only subscriber (no CRM/splynx link) can
    refer a friend on the native path — the mirror path structurally 409s
    for them (resolve_crm_subscriber_id → None)."""
    sub = _subscriber(db_session)
    assert getattr(sub, "splynx_customer_id", None) in (None, "")
    principal = {"principal_type": "subscriber", "subscriber_id": str(sub.id)}
    monkeypatch.setattr(referrals_service, "native_write_enabled", lambda db: True)
    out = me_api.my_refer_a_friend(
        ReferAFriendRequest(name="Native Friend", phone="08099887766"),
        db=db_session,
        principal=principal,
    )
    assert out["status"] == "pending"


def test_native_capture_is_duplicate_guarded(db_session, monkeypatch):
    """The native unique-active-referred-person guard holds through the
    route: referring the same friend twice does not create a second active
    referral row."""
    sub = _subscriber(db_session)
    principal = {"principal_type": "subscriber", "subscriber_id": str(sub.id)}
    monkeypatch.setattr(referrals_service, "native_write_enabled", lambda db: True)
    payload = ReferAFriendRequest(name="Same Friend", phone="08011112222")
    first = me_api.my_refer_a_friend(payload, db=db_session, principal=principal)
    second = me_api.my_refer_a_friend(payload, db=db_session, principal=principal)
    assert str(first["id"]) == str(second["id"])
    active = (
        db_session.query(Referral)
        .filter(Referral.referrer_subscriber_id == sub.id)
        .count()
    )
    assert active == 1
