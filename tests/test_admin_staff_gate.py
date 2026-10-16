"""Regression: the /admin web surface must default-deny non-staff principals.

Subscriber and reseller portal logins authenticate as ``principal_type ==
"subscriber"``; only staff are ``"system_user"``. The admin router previously
gated on authentication alone, so any authenticated principal could reach
admin-only routes (secret management, API-key minting, etc.). These tests pin
the staff gate in place.
"""

from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.models.subscriber import UserType
from app.models.system_user import SystemUser
from app.web.admin import router as admin_router
from app.web.auth.dependencies import require_admin_web_auth, require_web_auth


def _router_dependency_calls(router):
    return [getattr(dep, "dependency", None) for dep in router.dependencies]


def _request() -> Request:
    return Request({"type": "http", "method": "GET", "path": "/admin"})


def _require(auth, *, request: Request | None = None):
    return require_admin_web_auth(
        request=request or _request(), auth=auth, db=MagicMock()
    )


def test_admin_router_uses_staff_gate_not_bare_auth():
    calls = _router_dependency_calls(admin_router)
    assert require_admin_web_auth in calls, "/admin router must require the staff gate"
    # The bare authn-only dependency must not be the admin gate anymore.
    assert require_web_auth not in calls


def test_staff_gate_hydrates_admin_navigation_permissions():
    request = _request()
    auth = {
        "principal_type": "system_user",
        "principal_id": str(uuid4()),
        "roles": ["admin"],
        "scopes": [],
        "subscriber": SystemUser(
            first_name="Admin",
            last_name="User",
            email="admin@example.test",
            user_type=UserType.system_user,
            is_active=True,
        ),
    }

    result = _require(auth, request=request)

    assert result is auth
    assert request.state.auth is auth
    assert auth["permission_keys"] == frozenset({"*"})


def test_staff_gate_hydrates_restricted_staff_navigation_permissions(monkeypatch):
    request = _request()
    auth = {
        "principal_type": "system_user",
        "principal_id": str(uuid4()),
        "roles": ["support"],
        "scopes": [],
        "subscriber": SystemUser(
            first_name="Support",
            last_name="User",
            email="support@example.test",
            user_type=UserType.system_user,
            is_active=True,
        ),
    }

    def hydrate_permission_keys(auth_values, _db):
        auth_values["permission_keys"] = frozenset({"support:ticket:read"})
        return auth_values["permission_keys"]

    monkeypatch.setattr(
        "app.web.auth.dependencies.load_permission_keys", hydrate_permission_keys
    )

    result = _require(auth, request=request)

    assert result is auth
    assert request.state.auth is auth
    assert auth["permission_keys"] == frozenset({"support:ticket:read"})


def test_admin_router_has_session_refresh_probe():
    paths = {getattr(route, "path", "") for route in admin_router.routes}
    assert "/admin/session/refresh" in paths


def test_staff_gate_rejects_subscriber():
    with pytest.raises(HTTPException) as exc:
        _require({"principal_type": "subscriber", "principal_id": "p"})
    assert exc.value.status_code == 403


def test_staff_gate_rejects_missing_principal_type():
    with pytest.raises(HTTPException) as exc:
        _require({"principal_id": "p"})
    assert exc.value.status_code == 403


def test_staff_gate_allows_system_user():
    auth = {
        "principal_type": "system_user",
        "principal_id": str(uuid4()),
        "roles": ["admin"],
        "scopes": [],
        "subscriber": SystemUser(
            first_name="Admin",
            last_name="User",
            email="admin@example.test",
            user_type=UserType.system_user,
            is_active=True,
        ),
    }
    assert _require(auth) is auth


@pytest.mark.parametrize(
    "user_type", (UserType.customer, UserType.reseller, UserType.vendor)
)
def test_staff_gate_rejects_non_staff_system_user(user_type: UserType):
    auth = {
        "principal_type": "system_user",
        "principal_id": "p",
        "subscriber": SystemUser(
            first_name="Non",
            last_name="Staff",
            email=f"{user_type.value}@example.test",
            user_type=user_type,
            is_active=True,
        ),
    }
    with pytest.raises(HTTPException) as exc:
        _require(auth)
    assert exc.value.status_code == 403
