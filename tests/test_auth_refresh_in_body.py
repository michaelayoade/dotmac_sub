"""Unit tests for the mobile refresh-token transport gate.

Native clients send ``X-Auth-Refresh-In-Body`` so the refresh token is returned
in the JSON body (and stored in the platform secure store) instead of an
httpOnly cookie, which they cannot read.
"""

from types import SimpleNamespace

from app.services.auth_flow import _wants_refresh_in_body


def _req(headers: dict) -> SimpleNamespace:
    return SimpleNamespace(headers=headers)


def test_none_request_defaults_to_cookie():
    assert _wants_refresh_in_body(None) is False


def test_missing_header_defaults_to_cookie():
    assert _wants_refresh_in_body(_req({})) is False


def test_truthy_values_opt_into_body():
    for value in ("true", "True", "1", "yes", " TRUE "):
        assert _wants_refresh_in_body(_req({"x-auth-refresh-in-body": value})) is True


def test_falsey_values_keep_cookie():
    for value in ("0", "false", "no", ""):
        assert _wants_refresh_in_body(_req({"x-auth-refresh-in-body": value})) is False
