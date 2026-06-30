"""Customer saved cards + Paystack charge_authorization.

Paystack is mocked (no live keys), so the capture mapper, de-dup, self-scoped
management and the server-to-server charge are all exercised here.
"""

from __future__ import annotations

import pytest

import app.services.flutterwave as flutterwave
import app.services.paystack as paystack
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscription_engine import SettingValueType
from app.schemas.billing import PaymentMethodCreate
from app.services import billing as billing_service
from app.services import customer_portal_flow_payment_methods as cards
from app.services.settings_cache import SettingsCache
from app.services.settings_spec import get_spec

# --- Paystack charge_authorization ----------------------------------------


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_charge_authorization_posts_and_returns_data(monkeypatch, db_session):
    monkeypatch.setattr(paystack, "_get_secret_key", lambda db=None: "sk_test")
    db_session.add(
        DomainSetting(
            domain=SettingDomain.billing,
            key="payment_gateway_timeout_seconds",
            value_type=SettingValueType.integer,
            value_text="12",
            is_active=True,
        )
    )
    db_session.commit()
    SettingsCache.invalidate(
        SettingDomain.billing.value, "payment_gateway_timeout_seconds"
    )
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return _FakeResp(
            {"status": True, "data": {"status": "success", "reference": "R1"}}
        )

    monkeypatch.setattr(paystack.httpx, "post", fake_post)

    data = paystack.charge_authorization(
        db_session,
        authorization_code="AUTH_x",
        email="a@b.c",
        amount_kobo=500000,
        reference="R1",
    )
    assert data["status"] == "success"
    assert captured["url"].endswith("/transaction/charge_authorization")
    assert captured["json"]["authorization_code"] == "AUTH_x"
    assert captured["json"]["amount"] == 500000
    assert captured["timeout"] == 12


def test_payment_gateway_timeout_setting_specs_registered():
    spec = get_spec(SettingDomain.billing, "payment_gateway_timeout_seconds")
    assert spec is not None
    assert spec.default == 30
    assert spec.min_value == 1
    assert spec.max_value == 120


def test_flutterwave_uses_configured_gateway_timeout(monkeypatch, db_session):
    monkeypatch.setattr(flutterwave, "_get_secret_key", lambda db=None: "flw_sk")
    db_session.add(
        DomainSetting(
            domain=SettingDomain.billing,
            key="payment_gateway_timeout_seconds",
            value_type=SettingValueType.integer,
            value_text="9",
            is_active=True,
        )
    )
    db_session.commit()
    SettingsCache.invalidate(
        SettingDomain.billing.value, "payment_gateway_timeout_seconds"
    )
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["timeout"] = timeout
        return _FakeResp({"status": "success", "data": {"link": "https://pay.test"}})

    monkeypatch.setattr(flutterwave.httpx, "post", fake_post)

    data = flutterwave.initialize_transaction(
        db_session,
        email="a@b.c",
        amount=5000,
        reference="FLW-R1",
        redirect_url="https://dotmac.test/return",
    )

    assert data["link"] == "https://pay.test"
    assert captured["url"].endswith("/payments")
    assert captured["timeout"] == 9


def test_charge_authorization_raises_on_api_failure(monkeypatch, db_session):
    monkeypatch.setattr(paystack, "_get_secret_key", lambda db=None: "sk_test")
    monkeypatch.setattr(
        paystack.httpx,
        "post",
        lambda *a, **k: _FakeResp({"status": False, "message": "declined"}),
    )
    with pytest.raises(ValueError, match="declined"):
        paystack.charge_authorization(
            db_session,
            authorization_code="AUTH_x",
            email="a@b.c",
            amount_kobo=1,
            reference="R2",
        )


# --- capture mapper --------------------------------------------------------

_AUTH = {
    "authorization_code": "AUTH_abc",
    "last4": "4081",
    "exp_month": "08",
    "exp_year": "2030",
    "card_type": "visa ",
    "brand": "visa",
    "reusable": True,
}


def test_payment_method_from_authorization_maps_fields():
    import uuid

    pm = cards.payment_method_from_authorization(_AUTH, str(uuid.uuid4()))
    assert pm is not None
    assert pm.token == "AUTH_abc"
    assert pm.last4 == "4081"
    assert pm.expires_month == 8
    assert pm.expires_year == 2030
    assert pm.label == "Visa •••• 4081"


def test_payment_method_from_authorization_none_when_not_reusable():
    assert cards.payment_method_from_authorization({"reusable": False}, "a") is None
    assert cards.payment_method_from_authorization({}, "a") is None
    assert cards.payment_method_from_authorization(None, "a") is None


def test_save_card_dedups_on_token(db_session, subscriber):
    first = cards.save_card_from_authorization(db_session, str(subscriber.id), _AUTH)
    again = cards.save_card_from_authorization(db_session, str(subscriber.id), _AUTH)
    assert first is not None
    assert str(again.id) == str(first.id)
    assert len(cards.list_for_account(db_session, str(subscriber.id))) == 1


# --- self-scoped management ------------------------------------------------


def _make_card(db_session, account_id, *, last4: str, default=False):
    return billing_service.payment_methods.create(
        db_session,
        PaymentMethodCreate(
            account_id=account_id,
            label=f"Visa •••• {last4}",
            token=f"AUTH_{last4}",
            last4=last4,
            brand="visa",
            is_default=default,
        ),
    )


def test_set_default_unsets_others_and_is_owner_scoped(db_session, subscriber):
    a = _make_card(db_session, subscriber.id, last4="1111", default=True)
    b = _make_card(db_session, subscriber.id, last4="2222")

    cards.set_default(db_session, str(subscriber.id), str(b.id))
    db_session.refresh(a)
    db_session.refresh(b)
    assert b.is_default is True
    assert a.is_default is False


def test_remove_is_owner_scoped(db_session, subscriber):
    from app.models.subscriber import Subscriber

    stranger = Subscriber(first_name="Other", last_name="User", email="o@x.io")
    db_session.add(stranger)
    db_session.commit()

    mine = _make_card(db_session, subscriber.id, last4="3333")
    theirs = _make_card(db_session, stranger.id, last4="4444")

    # cannot touch a card on another account
    assert cards.remove(db_session, str(subscriber.id), str(theirs.id)) is False
    assert cards.set_default(db_session, str(subscriber.id), str(theirs.id)) is None
    # can remove own
    assert cards.remove(db_session, str(subscriber.id), str(mine.id)) is True
    remaining = cards.list_for_account(db_session, str(subscriber.id))
    assert all(str(m.id) != str(mine.id) for m in remaining)


def test_list_excludes_other_accounts(db_session, subscriber):
    _make_card(db_session, subscriber.id, last4="5555")
    listed = cards.list_for_account(db_session, str(subscriber.id))
    assert len(listed) == 1
    assert listed[0].last4 == "5555"
    # tokens are stored encrypted, not equal to the raw code
    assert listed[0].token != "AUTH_5555"
