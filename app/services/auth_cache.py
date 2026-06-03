from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

from app.services import app_cache

_AUTH_NAMESPACE = "auth"
_CLAIMS_TTL_SECONDS = 300
_SESSION_TTL_SECONDS = 120


def _claims_ttl_seconds() -> int:
    raw = os.getenv("AUTH_RBAC_CLAIMS_CACHE_TTL_SECONDS", str(_CLAIMS_TTL_SECONDS))
    try:
        return max(30, int(raw))
    except ValueError:
        return _CLAIMS_TTL_SECONDS


def _session_ttl_seconds() -> int:
    raw = os.getenv("AUTH_SESSION_CACHE_TTL_SECONDS", str(_SESSION_TTL_SECONDS))
    try:
        return max(30, int(raw))
    except ValueError:
        return _SESSION_TTL_SECONDS


def _claims_key(principal_type: str, principal_id: str) -> str:
    return app_cache.cache_key(_AUTH_NAMESPACE, "claims", principal_type, principal_id)


def _session_key(session_id: str) -> str:
    return app_cache.cache_key(_AUTH_NAMESPACE, "session", session_id)


def _principal_sessions_key(principal_type: str, principal_id: str) -> str:
    return app_cache.cache_key(
        _AUTH_NAMESPACE, "principal-sessions", principal_type, principal_id
    )


def _user_snapshot(principal: object) -> dict[str, Any]:
    return {
        "id": str(getattr(principal, "id", "") or ""),
        "person_id": str(getattr(principal, "person_id", "") or ""),
        "first_name": str(getattr(principal, "first_name", "") or ""),
        "last_name": str(getattr(principal, "last_name", "") or ""),
        "display_name": str(getattr(principal, "display_name", "") or ""),
        "email": str(getattr(principal, "email", "") or ""),
    }


def principal_from_snapshot(snapshot: dict[str, Any]) -> object:
    return SimpleNamespace(
        id=snapshot.get("id"),
        person_id=snapshot.get("person_id"),
        first_name=snapshot.get("first_name"),
        last_name=snapshot.get("last_name"),
        display_name=snapshot.get("display_name"),
        email=snapshot.get("email"),
    )


def get_claims(
    principal_type: str, principal_id: str
) -> tuple[list[str], list[str]] | None:
    payload = app_cache.get_json(_claims_key(principal_type, principal_id))
    if not isinstance(payload, dict):
        return None
    roles = payload.get("roles")
    scopes = payload.get("scopes")
    if not isinstance(roles, list) or not isinstance(scopes, list):
        return None
    return [str(role) for role in roles], [str(scope) for scope in scopes]


def set_claims(
    principal_type: str,
    principal_id: str,
    roles: list[str],
    scopes: list[str],
    ttl_seconds: int | None = None,
) -> bool:
    ttl = ttl_seconds or _claims_ttl_seconds()
    return app_cache.set_json(
        _claims_key(principal_type, principal_id),
        {"roles": list(roles), "scopes": list(scopes)},
        ttl,
    )


def get_session_context(session_id: str) -> dict[str, Any] | None:
    payload = app_cache.get_json(_session_key(session_id))
    return payload if isinstance(payload, dict) else None


def set_session_context(
    *,
    session_id: str,
    principal_type: str,
    principal_id: str,
    roles: list[str],
    scopes: list[str],
    principal: object,
    ttl_seconds: int | None = None,
) -> bool:
    default_ttl = _session_ttl_seconds()
    ttl = default_ttl
    if ttl_seconds is not None:
        ttl = max(30, min(int(ttl_seconds), default_ttl))
    app_cache.sadd(
        _principal_sessions_key(principal_type, principal_id),
        session_id,
        ttl_seconds=ttl,
    )
    return app_cache.set_json(
        _session_key(session_id),
        {
            "principal_type": principal_type,
            "principal_id": principal_id,
            "session_id": session_id,
            "roles": list(roles),
            "scopes": list(scopes),
            "user": _user_snapshot(principal),
        },
        ttl,
    )


def invalidate_session_context(
    session_id: str,
    *,
    principal_type: str | None = None,
    principal_id: str | None = None,
) -> None:
    app_cache.delete_key(_session_key(session_id))
    if principal_type and principal_id:
        app_cache.srem(
            _principal_sessions_key(principal_type, principal_id), session_id
        )


def invalidate_principal(principal_type: str, principal_id: str) -> int:
    deleted = 0
    session_ids = app_cache.smembers(
        _principal_sessions_key(principal_type, principal_id)
    )
    if session_ids:
        deleted += app_cache.delete_many(
            [_session_key(session_id) for session_id in session_ids]
        )
    app_cache.delete_key(_principal_sessions_key(principal_type, principal_id))
    if app_cache.delete_key(_claims_key(principal_type, principal_id)):
        deleted += 1
    return deleted


def invalidate_all_auth_cache() -> int:
    return app_cache.scan_delete(app_cache.cache_key(_AUTH_NAMESPACE))
