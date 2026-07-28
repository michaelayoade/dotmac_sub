"""Customer portal Refer & Earn web route (RFC #73): auth gate + delegation."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db import get_db
from app.services.referrals import ReferralProgramError
from app.web.customer.referrals import router


def _client(db_session):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def test_get_redirects_to_login_when_anonymous(db_session):
    client = _client(db_session)
    with patch(
        "app.web.customer.referrals.get_current_customer_from_request",
        return_value=None,
    ):
        r = client.get("/portal/refer-and-earn", follow_redirects=False)
    assert r.status_code == 303
    assert "/portal/auth/login" in r.headers["location"]


def test_post_refers_a_friend_and_redirects(db_session):
    client = _client(db_session)
    subscriber_id = uuid.uuid4()
    with (
        patch(
            "app.web.customer.referrals.get_current_customer_from_request",
            return_value={"subscriber_id": str(subscriber_id)},
        ),
        patch(
            "app.web.customer.referrals.optional_customer_subscriber_id",
            return_value=subscriber_id,
        ),
        patch(
            "app.web.customer.referrals.referrals_service.refer_friend",
            return_value=SimpleNamespace(referral_id=uuid.uuid4(), status="pending"),
        ) as refer,
    ):
        r = client.post(
            "/portal/refer-and-earn",
            data={"email": "friend@example.com"},
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert "referred=1" in r.headers["location"]
    refer.assert_called_once()
    assert refer.call_args.args[1].email == "friend@example.com"


def test_post_surfaces_referral_error(db_session):
    client = _client(db_session)
    subscriber_id = uuid.uuid4()
    with (
        patch(
            "app.web.customer.referrals.get_current_customer_from_request",
            return_value={"subscriber_id": str(subscriber_id)},
        ),
        patch(
            "app.web.customer.referrals.optional_customer_subscriber_id",
            return_value=subscriber_id,
        ),
        patch(
            "app.web.customer.referrals.referrals_service.refer_friend",
            side_effect=ReferralProgramError(
                code="referrals.program.contact_required",
                message="An email or phone number is required.",
            ),
        ),
    ):
        r = client.post(
            "/portal/refer-and-earn",
            data={"name": "No Contact"},
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert "error=" in r.headers["location"]
