"""The share link resolves to the public referral signup flow."""

from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.crm_referrals import public_router as referral_api_router
from app.db import get_db
from app.models.domain_settings import DomainSetting, SettingDomain, SettingValueType
from app.models.subscriber import Subscriber, SubscriberStatus
from app.web.public.referrals import router
from tests.referral_program_testkit import ensure_code


def _client(db_session):
    app = FastAPI()
    app.include_router(router)
    app.include_router(referral_api_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def test_valid_link_renders_capture_and_signup_flow(db_session):
    with (
        patch(
            "app.web.public.referrals.referrals.get_by_code",
            return_value=SimpleNamespace(code="BWQF67MN"),
        ),
        patch(
            "app.web.public.referrals.referrals.program", return_value={"enabled": True}
        ),
    ):
        response = _client(db_session).get("/r/bwqf67mn")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "/api/v1/referrals/capture" in response.text
    assert "/api/v1/referrals/signup" in response.text
    assert "BWQF67MN" in response.text


def test_unknown_link_is_unavailable(db_session):
    with patch("app.web.public.referrals.referrals.get_by_code", return_value=None):
        response = _client(db_session).get("/r/UNKNOWN")

    assert response.status_code == 404
    assert "This referral link is unavailable" in response.text
    assert "signup-form" not in response.text


def test_referral_link_capture_and_signup_create_the_exact_account(db_session):
    db_session.add(
        DomainSetting(
            domain=SettingDomain.subscriber,
            key="referral_program_enabled",
            value_type=SettingValueType.boolean,
            value_text="true",
            is_active=True,
        )
    )
    referrer = Subscriber(
        first_name="Referral",
        last_name="Owner",
        email=f"referrer-{uuid4().hex}@example.test",
        status=SubscriberStatus.active,
        is_active=True,
    )
    db_session.add(referrer)
    db_session.commit()
    code = ensure_code(db_session, referrer.id)
    friend_email = f"friend-{uuid4().hex}@example.test"
    client = _client(db_session)

    page = client.get(f"/r/{code.code}")
    assert page.status_code == 200

    captured = client.post(
        "/api/v1/referrals/capture",
        json={"code": code.code, "name": "Friend Customer", "email": friend_email},
    )
    assert captured.status_code == 201
    capture = captured.json()
    signed_up = client.post(
        "/api/v1/referrals/signup",
        json={
            "conversion_token": capture["conversion_token"],
            "account": {
                "first_name": "Friend",
                "last_name": "Customer",
                "email": friend_email,
            },
        },
    )
    assert signed_up.status_code == 201
    result = signed_up.json()
    assert result["referral_id"] == capture["id"]
    account = db_session.get(Subscriber, result["subscriber_id"])
    assert account is not None
    assert account.email == friend_email
    assert account.status == SubscriberStatus.new
