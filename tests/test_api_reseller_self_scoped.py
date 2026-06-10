"""Unit tests for the self-scoped reseller endpoints in app/api/reseller.py.

They verify the endpoint layer's responsibilities: reject non-reseller
principals (403), force the caller's own reseller_id as the scope passed to the
services, and surface a foreign account (service returns None) as 404.
"""

import uuid

import pytest
from fastapi import HTTPException

from app.api import reseller as reseller_api


def _subscriber_principal():
    return {"principal_type": "subscriber", "subscriber_id": str(uuid.uuid4())}


def _system_user_principal():
    return {"principal_type": "system_user", "subscriber_id": str(uuid.uuid4())}


def test_reseller_id_rejects_non_subscriber():
    with pytest.raises(HTTPException) as exc:
        reseller_api._reseller_id(None, _system_user_principal())
    assert exc.value.status_code == 403


def test_reseller_id_rejects_subscriber_without_reseller(monkeypatch):
    monkeypatch.setattr(
        reseller_api.reseller_portal,
        "reseller_id_for_subscriber",
        lambda db, sid: None,
    )
    with pytest.raises(HTTPException) as exc:
        reseller_api._reseller_id(None, _subscriber_principal())
    assert exc.value.status_code == 403


def test_reseller_id_returns_for_reseller(monkeypatch):
    monkeypatch.setattr(
        reseller_api.reseller_portal,
        "reseller_id_for_subscriber",
        lambda db, sid: "reseller-1",
    )
    assert reseller_api._reseller_id(None, _subscriber_principal()) == "reseller-1"


def test_accounts_scopes_to_caller_reseller(monkeypatch):
    principal = _subscriber_principal()
    captured = {}

    monkeypatch.setattr(
        reseller_api.reseller_portal,
        "reseller_id_for_subscriber",
        lambda db, sid: "reseller-1",
    )

    def fake_list(db, reseller_id, limit, offset, search):
        captured["reseller_id"] = reseller_id
        return []

    monkeypatch.setattr(reseller_api.reseller_portal, "list_accounts", fake_list)
    monkeypatch.setattr(
        reseller_api.reseller_portal, "count_accounts", lambda db, rid, search: 0
    )

    out = reseller_api.my_reseller_accounts(
        search=None, limit=50, offset=0, db=None, principal=principal
    )
    assert captured["reseller_id"] == "reseller-1"
    assert out == {"items": [], "count": 0, "limit": 50, "offset": 0}


def test_accounts_403_for_non_reseller():
    with pytest.raises(HTTPException) as exc:
        reseller_api.my_reseller_accounts(
            search=None,
            limit=50,
            offset=0,
            db=None,
            principal=_system_user_principal(),
        )
    assert exc.value.status_code == 403


def test_account_detail_404_for_foreign_account(monkeypatch):
    monkeypatch.setattr(
        reseller_api.reseller_portal,
        "reseller_id_for_subscriber",
        lambda db, sid: "reseller-1",
    )
    # The service returns None for an account that isn't this reseller's.
    monkeypatch.setattr(
        reseller_api.reseller_portal,
        "get_account_detail",
        lambda db, reseller_id, account_id: None,
    )
    with pytest.raises(HTTPException) as exc:
        reseller_api.my_reseller_account(
            account_id=str(uuid.uuid4()), db=None, principal=_subscriber_principal()
        )
    assert exc.value.status_code == 404


def test_account_tickets_404_for_foreign_account(monkeypatch):
    monkeypatch.setattr(
        reseller_api.reseller_portal,
        "reseller_id_for_subscriber",
        lambda db, sid: "res-1",
    )
    monkeypatch.setattr(
        reseller_api.reseller_portal,
        "get_account_detail",
        lambda db, reseller_id, account_id: None,
    )
    with pytest.raises(HTTPException) as exc:
        reseller_api.my_reseller_account_tickets(
            account_id=str(uuid.uuid4()),
            db=None,
            principal=_subscriber_principal(),
        )
    assert exc.value.status_code == 404


def test_account_tickets_soft_fails_when_crm_down(monkeypatch):
    from app.services import crm_portal
    from app.services.crm_client import CRMClientError

    monkeypatch.setattr(
        reseller_api.reseller_portal,
        "reseller_id_for_subscriber",
        lambda db, sid: "res-1",
    )
    monkeypatch.setattr(
        reseller_api.reseller_portal,
        "get_account_detail",
        lambda db, reseller_id, account_id: {"id": account_id},
    )

    def _boom(db, account_id):
        raise CRMClientError("down")

    monkeypatch.setattr(crm_portal, "resolve_crm_subscriber_id", _boom)

    out = reseller_api.my_reseller_account_tickets(
        account_id=str(uuid.uuid4()),
        db=None,
        principal=_subscriber_principal(),
    )
    assert out == {"items": [], "crm_available": False}


def test_account_tickets_normalizes_crm_fields(monkeypatch):
    from app.services import crm_portal

    monkeypatch.setattr(
        reseller_api.reseller_portal,
        "reseller_id_for_subscriber",
        lambda db, sid: "res-1",
    )
    monkeypatch.setattr(
        reseller_api.reseller_portal,
        "get_account_detail",
        lambda db, reseller_id, account_id: {"id": account_id},
    )
    monkeypatch.setattr(
        crm_portal, "resolve_crm_subscriber_id", lambda db, account_id: "crm-9"
    )

    class _Client:
        def list_tickets(self, subscriber_id):
            assert subscriber_id == "crm-9"
            return [
                {
                    "name": "TCK-1",
                    "title": "No internet",
                    "status": "open",
                    "creation": "2026-06-01T10:00:00",
                }
            ]

    monkeypatch.setattr(crm_portal, "get_crm_client", lambda: _Client())

    out = reseller_api.my_reseller_account_tickets(
        account_id=str(uuid.uuid4()),
        db=None,
        principal=_subscriber_principal(),
    )
    assert out["crm_available"] is True
    assert out["items"] == [
        {
            "id": "TCK-1",
            "subject": "No internet",
            "status": "open",
            "priority": None,
            "created_at": "2026-06-01T10:00:00",
            "updated_at": None,
        }
    ]


def test_profile_403_for_non_reseller(monkeypatch):
    monkeypatch.setattr(
        reseller_api.reseller_portal,
        "reseller_id_for_subscriber",
        lambda db, sid: None,
    )
    with pytest.raises(HTTPException) as exc:
        reseller_api.my_reseller_profile(db=None, principal=_subscriber_principal())
    assert exc.value.status_code == 403


def test_profile_roundtrip_and_update(db_session, monkeypatch):
    from app.models.subscriber import Reseller, Subscriber, UserType

    sub = Subscriber(
        first_name="Res",
        last_name="Eller",
        email="reseller.profile@example.com",
        user_type=UserType.reseller,
    )
    reseller = Reseller(name="Acme Networks", code="ACME")
    db_session.add_all([sub, reseller])
    db_session.commit()

    monkeypatch.setattr(
        reseller_api.reseller_portal,
        "reseller_id_for_subscriber",
        lambda db, sid: str(reseller.id),
    )
    principal = {"principal_type": "subscriber", "subscriber_id": str(sub.id)}

    out = reseller_api.my_reseller_profile(db=db_session, principal=principal)
    assert out["name"] == "Acme Networks"
    assert out["mfa_enabled"] is False

    updated = reseller_api.my_reseller_profile_update(
        payload=reseller_api.ResellerProfileUpdate(
            contact_email="  ops@acme.example  ", contact_phone=""
        ),
        db=db_session,
        principal=principal,
    )
    assert updated["contact_email"] == "ops@acme.example"
    assert updated["contact_phone"] is None  # blank clears the field


def test_mfa_setup_and_confirm_flow(db_session, monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("TOTP_ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    from app.models.subscriber import Reseller, Subscriber, UserType

    sub = Subscriber(
        first_name="Mfa",
        last_name="User",
        email="reseller.mfa@example.com",
        user_type=UserType.reseller,
    )
    reseller = Reseller(name="Mfa Networks")
    db_session.add_all([sub, reseller])
    db_session.commit()

    monkeypatch.setattr(
        reseller_api.reseller_portal,
        "reseller_id_for_subscriber",
        lambda db, sid: str(reseller.id),
    )
    principal = {"principal_type": "subscriber", "subscriber_id": str(sub.id)}

    setup = reseller_api.my_reseller_mfa_setup(db=db_session, principal=principal)
    assert setup["method_id"] and setup["secret"] and setup["otpauth_uri"]

    # Wrong code -> 400, method stays unverified.
    with pytest.raises(HTTPException) as exc:
        reseller_api.my_reseller_mfa_confirm(
            payload=reseller_api.MfaConfirmRequest(
                method_id=setup["method_id"], code="000000"
            ),
            db=db_session,
            principal=principal,
        )
    assert exc.value.status_code == 400

    # Correct TOTP -> enabled.
    import pyotp

    code = pyotp.TOTP(setup["secret"]).now()
    out = reseller_api.my_reseller_mfa_confirm(
        payload=reseller_api.MfaConfirmRequest(method_id=setup["method_id"], code=code),
        db=db_session,
        principal=principal,
    )
    assert out["mfa_enabled"] is True
