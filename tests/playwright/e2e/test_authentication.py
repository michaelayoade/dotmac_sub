from __future__ import annotations

from tests.playwright.helpers.api import api_get, bearer_headers


def test_admin_authentication(api_context, admin_token: str):
    response = api_get(api_context, "/api/v1/auth/me", headers=bearer_headers(admin_token))
    assert response.ok
    payload = response.json()
    assert "admin" in payload.get("roles", [])


def test_agent_authentication(api_context, agent_token: str):
    response = api_get(api_context, "/api/v1/auth/me", headers=bearer_headers(agent_token))
    assert response.ok
    payload = response.json()
    assert "support" in payload.get("roles", [])


def test_user_authentication(api_context, user_token: str):
    response = api_get(api_context, "/api/v1/auth/me", headers=bearer_headers(user_token))
    assert response.ok
    payload = response.json()
    assert "support" not in payload.get("roles", [])
