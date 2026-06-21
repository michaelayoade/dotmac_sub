"""Layer 3 Phase 1b — reseller portal pages work for a subscriber-less
first-class ResellerUser principal (no `context["subscriber"]`).

Drives the real page handlers with a real `get_context()` result for a
reseller_user login (flag ON), patching only template rendering. Verifies the
previously subscriber-dereferencing paths (profile/MFA, billing saved-cards,
contacts) no longer crash and behave correctly, and that the legacy
subscriber-backed path is unchanged.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pyotp
import pytest
from cryptography.fernet import Fernet
from starlette.requests import Request

from app.config import settings
from app.models.auth import MFAMethod, MFAMethodType
from app.models.subscriber import Reseller
from app.services import (
    auth_flow as auth_flow_service,
)
from app.services import (
    reseller_portal,
    web_reseller_billing,
    web_reseller_contacts,
    web_reseller_routes,
)


def _request():
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/reseller",
            "headers": [(b"user-agent", b"pytest")],
            "client": ("127.0.0.1", 5555),
            "query_string": b"",
        }
    )


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("TOTP_ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    old = settings.reseller_user_principal_enabled
    object.__setattr__(settings, "reseller_user_principal_enabled", True)
    yield
    object.__setattr__(settings, "reseller_user_principal_enabled", old)


@pytest.fixture()
def reseller_user_ctx(db_session, env):
    """A real reseller portal context for a subscriber-less reseller_user."""
    r = Reseller(name="ABC Networks", code="ABCP1B")
    db_session.add(r)
    db_session.commit()
    db_session.refresh(r)
    reseller_portal.create_reseller_user_principal(
        db_session,
        reseller_id=str(r.id),
        username="abc-admin",
        password="secret",  # noqa: S106
        email="owner@abcnetworks.com",
        full_name="ABC Owner",
    )
    result = reseller_portal.login(
        db_session, "abc-admin", "secret", _request(), remember=False
    )
    ctx = reseller_portal.get_context(db_session, result["session_token"])
    assert ctx["subscriber"] is None  # the whole point
    assert ctx["principal_type"] == "reseller_user"
    return ctx


def _capture_render(module):
    """Patch a route module's TemplateResponse; return the captured context dict."""
    captured = {}

    def _fake(template_name, context, *a, **k):
        captured["template"] = template_name
        captured["context"] = context
        resp = MagicMock(name="template_response")
        resp.status_code = k.get("status_code", 200)
        return resp

    return patch.object(
        module.templates, "TemplateResponse", side_effect=_fake
    ), captured


# --- Profile + MFA --------------------------------------------------------


def test_profile_page_renders_for_reseller_user(db_session, reseller_user_ctx):
    p, captured = _capture_render(web_reseller_routes)
    with (
        p,
        patch.object(
            web_reseller_routes,
            "_require_reseller_context",
            return_value=reseller_user_ctx,
        ),
    ):
        web_reseller_routes.reseller_profile(_request(), db_session)
    assert captured["context"]["active_page"] == "profile"
    assert captured["context"]["subscriber"] is None
    assert captured["context"]["mfa_methods"] == []  # no crash on subscriber.id


def test_reseller_user_mfa_setup_and_confirm(db_session, reseller_user_ctx):
    ru_id = reseller_user_ctx["principal_id"]
    # Setup
    p, _ = _capture_render(web_reseller_routes)
    with (
        p,
        patch.object(
            web_reseller_routes,
            "_require_reseller_context",
            return_value=reseller_user_ctx,
        ),
    ):
        web_reseller_routes.reseller_mfa_setup(_request(), db_session)
    method = (
        db_session.query(MFAMethod)
        .filter(MFAMethod.reseller_user_id == ru_id)
        .filter(MFAMethod.method_type == MFAMethodType.totp)
        .one()
    )
    assert method.subscriber_id is None
    assert method.enabled is False  # pending until confirmed

    # Confirm with a valid TOTP code
    secret = auth_flow_service._decrypt_secret(db_session, method.secret)
    code = pyotp.TOTP(secret).now()
    p2, _ = _capture_render(web_reseller_routes)
    with (
        p2,
        patch.object(
            web_reseller_routes,
            "_require_reseller_context",
            return_value=reseller_user_ctx,
        ),
    ):
        web_reseller_routes.reseller_mfa_confirm(
            _request(), db_session, str(method.id), code
        )
    db_session.refresh(method)
    assert method.enabled is True
    assert method.is_primary is True


def test_resend_email_verification_is_noop_for_reseller_user(
    db_session, reseller_user_ctx
):
    with patch.object(
        web_reseller_routes, "_require_reseller_context", return_value=reseller_user_ctx
    ):
        resp = web_reseller_routes.reseller_resend_email_verification(
            _request(), db_session
        )
    # Redirects with verify_sent=0 (no subscriber to verify), no crash.
    assert resp.status_code == 303
    assert "verify_sent=0" in resp.headers["location"]


# --- Billing (saved cards keyed on login subscriber) ----------------------


def test_billing_overview_empty_cards_for_reseller_user(db_session, reseller_user_ctx):
    p, captured = _capture_render(web_reseller_billing)
    with (
        p,
        patch.object(
            web_reseller_billing,
            "_require_reseller_context",
            return_value=reseller_user_ctx,
        ),
    ):
        web_reseller_billing.billing_overview(_request(), db_session)
    assert captured["context"]["saved_cards"] == []


def test_login_subscriber_id_none_for_reseller_user(reseller_user_ctx):
    assert web_reseller_billing._login_subscriber_id(reseller_user_ctx) is None


# --- Contacts (login-subscriber-keyed) ------------------------------------


def test_contacts_page_degrades_for_reseller_user(db_session, reseller_user_ctx):
    p, captured = _capture_render(web_reseller_contacts)
    with (
        p,
        patch.object(
            web_reseller_contacts,
            "_require_reseller_context",
            return_value=reseller_user_ctx,
        ),
    ):
        web_reseller_contacts.reseller_contacts(_request(), db_session)
    assert captured["context"]["contacts"] == []
    assert captured["context"].get("notice")


def test_contacts_create_rejected_for_reseller_user(db_session, reseller_user_ctx):
    p, captured = _capture_render(web_reseller_contacts)
    with (
        p,
        patch.object(
            web_reseller_contacts,
            "_require_reseller_context",
            return_value=reseller_user_ctx,
        ),
    ):
        resp = web_reseller_contacts.reseller_contacts_create(
            _request(), db_session, full_name="X", contact_type="billing"
        )
    assert resp.status_code == 400
    assert captured["context"].get("error")


# --- Regression: subscriber-backed reseller path still works --------------


def test_mfa_methods_helper_subscriber_path(db_session):
    ctx = {
        "principal_type": "subscriber",
        "principal_id": "00000000-0000-0000-0000-000000000001",
    }
    # Just must not raise and must scope by subscriber_id.
    assert web_reseller_routes._reseller_mfa_methods(db_session, ctx) == []
