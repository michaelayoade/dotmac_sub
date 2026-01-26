from __future__ import annotations

from typing import Any

import pytest

from app.services.auth_flow import hash_password

import re

from tests.playwright.helpers.api import (
    api_get,
    api_post_form,
    api_post_json,
    bearer_headers,
)


class AuthError(RuntimeError):
    pass


def login_for_token(api_context, username: str, password: str) -> str:
    response = api_post_json(
        api_context,
        "/api/v1/auth/login",
        {"username": username, "password": password},
    )
    if response.status == 404:
        return _login_for_token_via_web(api_context, username, password)
    if not response.ok:
        raise AuthError(f"Login failed for {username}: {response.status}")
    payload = response.json()
    if payload.get("mfa_required"):
        pytest.skip("MFA is enabled for this account; disable MFA for E2E users.")
    token = payload.get("access_token")
    if not token:
        raise AuthError("Login response missing access_token")
    return token


def _login_for_token_via_web(api_context, username: str, password: str) -> str:
    response = api_post_form(
        api_context,
        "/auth/login",
        {"username": username, "password": password, "remember": False},
        follow_redirects=False,
    )
    if response.status not in {200, 302, 303}:
        raise AuthError(f"Web login failed for {username}: {response.status}")

    token = _session_token_from_headers(response.headers)
    if not token:
        raise AuthError("Web login response missing session_token cookie")
    return token


def _session_token_from_headers(headers: dict[str, str]) -> str | None:
    cookie_header = headers.get("set-cookie") or headers.get("Set-Cookie")
    if not cookie_header:
        return None
    match = re.search(r"session_token=([^;]+)", cookie_header)
    if not match:
        return None
    return match.group(1)


def ensure_person(api_context, token: str, first_name: str, last_name: str, email: str) -> dict[str, Any]:
    headers = bearer_headers(token)
    response = api_get(api_context, f"/api/v1/people?email={email}", headers=headers)
    if not response.ok:
        raise AuthError(f"Failed to list people: {response.status}")
    data = response.json()
    items = data.get("items", [])
    if items:
        return items[0]

    response = api_post_json(
        api_context,
        "/api/v1/people",
        {"first_name": first_name, "last_name": last_name, "email": email},
        headers=headers,
    )
    if not response.ok:
        raise AuthError(f"Failed to create person: {response.status}")
    return response.json()


def ensure_user_credential(
    api_context,
    token: str,
    person_id: str,
    username: str,
    password: str,
) -> dict[str, Any]:
    headers = bearer_headers(token)
    response = api_get(
        api_context,
        f"/api/v1/user-credentials?person_id={person_id}",
        headers=headers,
    )
    if not response.ok:
        raise AuthError(f"Failed to list credentials: {response.status}")
    data = response.json()
    for cred in data.get("items", []):
        if cred.get("username") == username and cred.get("is_active"):
            return cred

    response = api_post_json(
        api_context,
        "/api/v1/user-credentials",
        {
            "person_id": person_id,
            "username": username,
            "password_hash": hash_password(password),
            "must_change_password": False,
            "is_active": True,
        },
        headers=headers,
    )
    if not response.ok:
        raise AuthError(f"Failed to create credential: {response.status}")
    return response.json()


def ensure_role_id(api_context, token: str, role_name: str) -> str:
    headers = bearer_headers(token)
    response = api_get(api_context, "/api/v1/rbac/roles?limit=200", headers=headers)
    if not response.ok:
        raise AuthError(f"Failed to list roles: {response.status}")
    data = response.json()
    for role in data.get("items", []):
        if role.get("name") == role_name and role.get("is_active"):
            return role["id"]
    raise AuthError(f"Role not found: {role_name}")


def ensure_person_role(api_context, token: str, person_id: str, role_id: str) -> dict[str, Any]:
    headers = bearer_headers(token)
    response = api_get(
        api_context,
        f"/api/v1/rbac/person-roles?person_id={person_id}&role_id={role_id}",
        headers=headers,
    )
    if not response.ok:
        raise AuthError(f"Failed to list person roles: {response.status}")
    data = response.json()
    items = data.get("items", [])
    if items:
        return items[0]

    response = api_post_json(
        api_context,
        "/api/v1/rbac/person-roles",
        {"person_id": person_id, "role_id": role_id},
        headers=headers,
    )
    if not response.ok:
        raise AuthError(f"Failed to create person role: {response.status}")
    return response.json()
