from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.api import auth_flow as auth_flow_api


@pytest.fixture()
def auth_flow_test_app(db_session):
    app = FastAPI()
    app.state.limiter = auth_flow_api.limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.include_router(auth_flow_api.router, prefix="/api/v1")
    app.dependency_overrides[auth_flow_api.get_db] = lambda: db_session
    yield app
    app.dependency_overrides.clear()


@pytest.fixture()
async def auth_flow_client(auth_flow_test_app):
    transport = ASGITransport(
        app=auth_flow_test_app,
        client=("198.51.100.10", 54321),
    )
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        yield client


@pytest.mark.asyncio
async def test_login_returns_429_after_rapid_failed_attempts_from_same_ip(
    auth_flow_client: AsyncClient,
):
    payload = {
        "username": "non-existent-user@example.com",
        "password": "invalid-password",
        "provider": "local",
    }

    for _ in range(20):
        response = await auth_flow_client.post("/api/v1/auth/login", json=payload)
        assert response.status_code == 401

    response = await auth_flow_client.post("/api/v1/auth/login", json=payload)
    assert response.status_code == 429
