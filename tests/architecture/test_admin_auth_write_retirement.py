"""Generic administration cannot mint sessions or install MFA credentials."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.auth import router
from app.schemas import auth as schemas
from app.services import auth as services


@pytest.mark.parametrize("resource", ["sessions", "mfa-methods"])
@pytest.mark.parametrize("method", ["POST", "PATCH"])
def test_generic_admin_credential_writes_are_not_routable(
    resource: str, method: str
) -> None:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    path = f"/api/v1/{resource}"
    if method == "PATCH":
        path += "/00000000-0000-0000-0000-000000000001"
    # No auth or database override: dispatch itself must refuse the old route,
    # even if authorization were accidentally weakened in the parent mount.
    response = TestClient(app).request(
        method,
        path,
        json={
            "token_hash": "attacker-selected-hash",
            "secret": "attacker-selected-secret",
            "enabled": True,
            "is_primary": True,
        },
    )
    assert response.status_code == 405


@pytest.mark.parametrize("resource", ["sessions", "mfa-methods"])
def test_read_and_revoke_reset_adapters_remain(resource: str) -> None:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    paths = app.openapi()["paths"]
    assert set(paths[f"/api/v1/{resource}"]) == {"get"}
    parameter = "session_id" if resource == "sessions" else "method_id"
    assert set(paths[f"/api/v1/{resource}/{{{parameter}}}"]) == {"get", "delete"}


@pytest.mark.parametrize("service", [services.sessions, services.mfa_methods])
def test_retired_services_cannot_be_called_behind_another_adapter(
    service: services.Sessions | services.MFAMethods,
) -> None:
    assert not hasattr(service, "create")
    assert not hasattr(service, "update")


@pytest.mark.parametrize(
    "name", ["SessionCreate", "SessionUpdate", "MFAMethodCreate", "MFAMethodUpdate"]
)
def test_arbitrary_credential_write_contracts_are_removed(name: str) -> None:
    assert not hasattr(schemas, name)
