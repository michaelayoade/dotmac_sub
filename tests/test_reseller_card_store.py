"""Layer 3 #329 — reseller saved cards owned by the reseller org.

A first-class reseller_user login has no backing subscriber, so its saved cards
are owned by the reseller org (PaymentMethod.reseller_id) instead of an account.
Verifies the reseller-scoped card store + the owner-routed billing wrappers.
"""

from __future__ import annotations

from app.models.subscriber import Reseller
from app.services import customer_portal_flow_payment_methods as cards
from app.services import reseller_portal_billing
from app.services.credential_crypto import decrypt_credential

AUTH = {
    "authorization_code": "AUTH_xyz",
    "reusable": True,
    "last4": "4081",
    "brand": "visa",
    "exp_month": "12",
    "exp_year": "2030",
}


def _reseller(db, code="CARD"):
    r = Reseller(name=f"Card Co {code}", code=code)
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


def test_save_and_list_reseller_card(db_session):
    r = _reseller(db_session)
    m = cards.save_card_for_reseller(db_session, str(r.id), AUTH)
    assert m is not None
    assert m.reseller_id == r.id
    assert m.account_id is None
    # Token is stored encrypted, like the account path.
    assert decrypt_credential(m.token) == "AUTH_xyz"
    assert [x.id for x in cards.list_for_reseller(db_session, str(r.id))] == [m.id]


def test_save_reseller_card_dedupes_on_fingerprint(db_session):
    r = _reseller(db_session)
    a = cards.save_card_for_reseller(db_session, str(r.id), AUTH)
    b = cards.save_card_for_reseller(db_session, str(r.id), AUTH)
    assert a.id == b.id
    assert len(cards.list_for_reseller(db_session, str(r.id))) == 1


def test_set_default_and_remove_reseller_card(db_session):
    r = _reseller(db_session)
    m1 = cards.save_card_for_reseller(db_session, str(r.id), AUTH)
    m2 = cards.save_card_for_reseller(
        db_session,
        str(r.id),
        {**AUTH, "last4": "1111", "authorization_code": "AUTH_2"},
    )
    assert cards.set_default_for_reseller(db_session, str(r.id), str(m2.id)) is not None
    db_session.refresh(m1)
    db_session.refresh(m2)
    assert m2.is_default is True
    assert m1.is_default is False
    assert cards.remove_for_reseller(db_session, str(r.id), str(m2.id)) is True
    assert [x.id for x in cards.list_for_reseller(db_session, str(r.id))] == [m1.id]


def test_reseller_card_owner_isolation(db_session):
    r1 = _reseller(db_session, code="R1")
    r2 = _reseller(db_session, code="R2")
    m = cards.save_card_for_reseller(db_session, str(r1.id), AUTH)
    # r2 cannot see or mutate r1's card.
    assert cards.list_for_reseller(db_session, str(r2.id)) == []
    assert cards.set_default_for_reseller(db_session, str(r2.id), str(m.id)) is None
    assert cards.remove_for_reseller(db_session, str(r2.id), str(m.id)) is False


def test_billing_wrapper_routes_by_owner(db_session, subscriber):
    r = _reseller(db_session)
    rcard = cards.save_card_for_reseller(db_session, str(r.id), AUTH)
    # No login subscriber (reseller_user) → reseller-owned cards.
    assert [
        c.id
        for c in reseller_portal_billing.list_payment_methods(
            db_session, None, str(r.id)
        )
    ] == [rcard.id]
    # A login subscriber takes precedence (subscriber-backed reseller) → its
    # own account cards (none here), NOT the reseller-owned card.
    assert (
        reseller_portal_billing.list_payment_methods(
            db_session, str(subscriber.id), str(r.id)
        )
        == []
    )
