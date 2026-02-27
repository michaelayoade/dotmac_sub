import pytest
from starlette.requests import Request

from app.services import web_customer_auth as web_customer_auth_service


def _request_with_cookie(name: str, value: str) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/portal",
        "headers": [(b"cookie", f"{name}={value}".encode())],
    }
    return Request(scope)


@pytest.mark.parametrize(
    ("next_url", "fallback", "expected"),
    [
        ("/portal/dashboard", "/portal/dashboard", "/portal/dashboard"),
        (
            "/portal/dashboard?tab=usage",
            "/portal/dashboard",
            "/portal/dashboard?tab=usage",
        ),
        ("//evil.com/phish", "/portal/dashboard", "/portal/dashboard"),
        ("https://evil.com/phish", "/portal/dashboard", "/portal/dashboard"),
        ("", "/portal/dashboard", "/portal/dashboard"),
        (None, "/portal/dashboard", "/portal/dashboard"),
    ],
)
def test_safe_next_only_allows_root_relative(next_url, fallback, expected):
    assert web_customer_auth_service._safe_next(next_url, fallback) == expected


def test_get_current_customer_enriches_module_flags(monkeypatch):
    monkeypatch.setattr(
        web_customer_auth_service.customer_portal,
        "get_current_customer",
        lambda _token, _db: {"username": "alice"},
    )
    monkeypatch.setattr(
        web_customer_auth_service.module_manager_service,
        "load_module_states",
        lambda _db: {"billing": False},
    )
    monkeypatch.setattr(
        web_customer_auth_service.module_manager_service,
        "load_feature_states",
        lambda _db: {"services_view": False},
    )

    request = _request_with_cookie(
        web_customer_auth_service.customer_portal.SESSION_COOKIE_NAME,
        "session-token",
    )
    current = web_customer_auth_service.get_current_customer_from_request(
        request, db=object()
    )

    assert current is not None
    assert current["username"] == "alice"
    assert current["module_states"]["billing"] is False
    assert current["feature_states"]["services_view"] is False


def test_get_current_customer_returns_none_when_session_missing(monkeypatch):
    monkeypatch.setattr(
        web_customer_auth_service.customer_portal,
        "get_current_customer",
        lambda _token, _db: None,
    )
    request = _request_with_cookie(
        web_customer_auth_service.customer_portal.SESSION_COOKIE_NAME,
        "missing",
    )
    assert (
        web_customer_auth_service.get_current_customer_from_request(
            request, db=object()
        )
        is None
    )
