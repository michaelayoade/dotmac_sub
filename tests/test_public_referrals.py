"""The share link resolves to the public referral signup flow."""

from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db import get_db
from app.schemas.geocoding import ReverseGeocodeResult
from app.services.brand_profiles import ResolvedBrand
from app.web.public.referrals import router


def _client(db_session):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def _brand() -> ResolvedBrand:
    return ResolvedBrand(
        name="Example Fiber",
        product_name="Example Fiber",
        legal_name="Example Fiber Ltd",
        tagline="Reliable connectivity",
        primary_color="#2457A6",
        secondary_color="#7C3AED",
        semantic_colors={},
        logo_url="/branding/assets/example-logo",
        dark_logo_url="/branding/assets/example-logo-dark",
        favicon_url="/branding/assets/example-favicon",
        support_email="help@example.test",
        support_phone="+2348000000000",
        from_email="hello@example.test",
        from_name="Example Fiber",
        app_url="https://selfcare.example.test",
        portal_domain="selfcare.example.test",
        legal_address={"city": "Abuja", "country": "Nigeria"},
        source_scope="platform",
        source_scope_id=None,
    )


def test_valid_link_renders_capture_and_signup_flow(db_session):
    with (
        patch(
            "app.web.public.referrals.referrals.get_by_code",
            return_value=SimpleNamespace(code="BWQF67MN"),
        ),
        patch(
            "app.web.public.referrals.referrals.program", return_value={"enabled": True}
        ),
        patch(
            "app.web.public.referrals.brand_profiles.resolve_brand",
            return_value=_brand(),
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
    assert "Type your service address, or use your current location" in response.text
    assert "Use my current location (optional)" in response.text
    assert "Address filled from your current location" in response.text
    assert "Your typed address will be used" in response.text
    assert 'data-state="neutral"' in response.text
    assert "position.coords.accuracy" in response.text
    assert "Type your service address to continue" in response.text
    assert "setFormBusy(true)" in response.text
    assert 'id="address-error"' in response.text
    assert 'id="first-name-error"' in response.text
    assert 'id="last-name-error"' in response.text
    assert 'id="email-error"' in response.text
    assert 'id="phone-error"' in response.text
    assert 'class="field-row"' in response.text
    assert 'id="location-spinner"' in response.text
    assert 'id="submit-spinner"' in response.text
    assert "Example Fiber · Refer &amp; Earn" in response.text
    assert "/branding/assets/example-logo" in response.text
    assert "/branding/assets/example-logo-dark" in response.text
    assert 'href="/branding/assets/example-favicon"' in response.text
    assert 'href="/static/css/main.css?v=20260829a"' in response.text
    assert 'href="/static/css/design-system.css?v=20260914a"' in response.text
    assert 'href="/branding/theme.css"' in response.text
    assert 'href="/branding/manifest.webmanifest"' in response.text
    assert "Apply dark mode immediately" in response.text
    assert "/static/branding/favicon/icon-192.png" not in response.text
    assert "system-ui" not in response.text
    assert "#11694d" not in response.text
    assert "Contact support" in response.text
    assert (
        "Use your current location before creating your account." not in response.text
    )


def test_location_lookup_fills_address_for_valid_referral(db_session):
    with (
        patch(
            "app.web.public.referrals.referrals.get_by_code",
            return_value=SimpleNamespace(code="LOOKUP01"),
        ),
        patch(
            "app.web.public.referrals.referrals.program", return_value={"enabled": True}
        ),
        patch(
            "app.web.public.referrals.geocoding.resolve_coordinates",
            return_value=ReverseGeocodeResult(
                display_name="12 Referral Close, Abuja",
                latitude=9.0765,
                longitude=7.3986,
            ),
        ),
    ):
        response = _client(db_session).get(
            "/r/LOOKUP01/reverse-geocode?latitude=9.0765&longitude=7.3986"
        )
    assert response.status_code == 200
    assert response.json() == {"display_name": "12 Referral Close, Abuja"}


def test_unknown_link_is_unavailable(db_session):
    with (
        patch("app.web.public.referrals.referrals.get_by_code", return_value=None),
        patch(
            "app.web.public.referrals.brand_profiles.resolve_brand",
            return_value=_brand(),
        ),
    ):
        response = _client(db_session).get("/r/UNKNOWN")

    assert response.status_code == 404
    assert "This referral link is unavailable" in response.text
    assert "signup-form" not in response.text
