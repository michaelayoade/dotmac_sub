"""Vendor-domain errors become HTTP responses only in the app adapter."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.errors import register_error_handlers
from app.services.vendor_portal_errors import VendorPortalOperationError
from app.web.auth.dependencies import AuthenticationRequired


@pytest.mark.parametrize(
    ("kind", "expected_status"),
    [
        ("invalid", 422),
        ("forbidden", 403),
        ("not_found", 404),
        ("conflict", 409),
    ],
)
def test_vendor_operation_error_maps_at_http_boundary(kind, expected_status):
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/api/vendor-error")
    def vendor_error():
        raise VendorPortalOperationError("vendor_test", "Rejected by owner", kind=kind)

    response = TestClient(app).get(
        "/api/vendor-error", headers={"accept": "application/json"}
    )

    assert response.status_code == expected_status
    assert response.json()["code"] == "vendor_test"
    assert response.json()["message"] == "Rejected by owner"


def test_vendor_auth_redirect_uses_shared_system_user_login() -> None:
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/vendor/projects")
    def protected_vendor_page() -> None:
        raise AuthenticationRequired()

    response = TestClient(app).get(
        "/vendor/projects?status=open",
        headers={"accept": "text/html"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == (
        "/auth/login?next=%2Fvendor%2Fprojects%3Fstatus%3Dopen"
    )
