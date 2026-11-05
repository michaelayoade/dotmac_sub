"""The share link resolves to the public referral signup flow."""

from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db import get_db
from app.web.public.referrals import router


def _client(db_session):
    app = FastAPI()
    app.include_router(router)
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
    assert 'name="address_line1"' in response.text
    assert 'id="capture-location"' in response.text
    assert "navigator.geolocation.getCurrentPosition" in response.text
    assert "Both are required" in response.text
    assert 'data-state="neutral"' in response.text
    assert "position.coords.accuracy" in response.text
    assert "Enable it in your browser settings" in response.text
    assert "setFormBusy(true)" in response.text
    assert 'id="address-error"' in response.text


def test_unknown_link_is_unavailable(db_session):
    with patch("app.web.public.referrals.referrals.get_by_code", return_value=None):
        response = _client(db_session).get("/r/UNKNOWN")

    assert response.status_code == 404
    assert "This referral link is unavailable" in response.text
    assert "signup-form" not in response.text
