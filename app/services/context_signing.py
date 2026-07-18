"""Shared cryptographic envelope for non-authentication domain capabilities.

Calling domains own token purpose, claims, lifetime, and consequences. This
adapter reuses the configured auth JWT key/algorithm and owns only signing and
signature/expiry verification. It does not authenticate a principal.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.services import auth_flow as auth_flow_service


def sign_context_token(db: Session | None, payload: dict[str, Any]) -> str:
    """Sign a typed, expiring domain capability envelope."""

    if not str(payload.get("typ") or "").strip():
        raise ValueError("Signed context requires a token type")
    if payload.get("iat") is None or payload.get("exp") is None:
        raise ValueError("Signed context requires issued-at and expiry claims")
    return auth_flow_service._jwt_encode_token(  # noqa: SLF001
        payload,
        auth_flow_service._jwt_secret(db),  # noqa: SLF001
        auth_flow_service._jwt_algorithm(db),  # noqa: SLF001
    )


def verify_context_token(db: Session | None, token: str) -> dict[Any, Any]:
    """Verify signature/expiry and return claims for domain-level validation."""

    return auth_flow_service._jwt_decode_token(  # noqa: SLF001
        token,
        auth_flow_service._jwt_secret(db),  # noqa: SLF001
        auth_flow_service._jwt_algorithm(db),  # noqa: SLF001
    )
