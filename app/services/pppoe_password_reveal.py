"""Audited, rate-limited, non-cacheable reveal of a PPPoE service password.

Revealing a reusable PPPoE credential is an explicit action: POST only, CSRF
protected, gated by ``subscription:credential:reveal``, audited, and never
cached. The secret is returned only in the response body and is never written
to the audit metadata or the logs.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

REVEAL_PERMISSION = "subscription:credential:reveal"
REVEAL_AUDIT_ACTION = "customer.pppoe_password_reveal"
NO_STORE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, private",
    "Pragma": "no-cache",
    "Expires": "0",
}


class CredentialUnusable(Exception):  # noqa: N818 - domain signal, not an error type
    """A stored credential must not be resolved or revealed (fail closed)."""

    def __init__(self, reason: str = "secret_reference") -> None:
        super().__init__(reason)
        self.reason = reason


UNUSABLE_MESSAGE = (
    "Credential unusable: the stored value is not a plain service password. "
    "It cannot be revealed or sent. Set a new service password."
)


def refuse_secret_reference(stored: str | None) -> None:
    """Raise ``CredentialUnusable`` if ``stored`` is a secret reference.

    A secret reference (``env://``, ``bao://``, ``vault://``, ``openbao://``...)
    makes ``decrypt_credential`` resolve a process or vault secret, so it must
    never be resolved on a staff-facing path.
    """
    if not stored:
        return
    from app.services.secrets import is_secret_ref

    if is_secret_ref(stored):
        raise CredentialUnusable("secret_reference")


def _json(payload: dict[str, object], status_code: int) -> JSONResponse:
    return JSONResponse(payload, status_code=status_code, headers=NO_STORE_HEADERS)


def audited_reveal(
    request: Request,
    db: Session,
    *,
    subscriber_id: str,
    metadata: dict[str, object],
    lookup: Callable[[], tuple[str, bool]],
) -> JSONResponse:
    """Run ``lookup`` (returns ``(password, found)``) with audit and rate limit."""
    from app.services import web_admin as web_admin_service
    from app.services.audit_adapter import AuditActor, record_audit_event
    from app.services.rate_limiter_adapter import allow_operation

    actor = web_admin_service.get_current_user(request)
    actor_id = web_admin_service.get_actor_id(request)
    audit_actor = AuditActor.user(
        actor_id or "unknown",
        label=actor.get("name") if actor else None,
    )
    actor_metadata = {
        key: value
        for key, value in {
            "actor_name": actor.get("name") if actor else None,
            "actor_email": actor.get("email") if actor else None,
        }.items()
        if value
    }

    decision = allow_operation(
        f"pppoe-reveal:{actor_id or 'unknown'}",
        limit=30,
        window_seconds=3600,
    )
    if not decision.allowed:
        record_audit_event(
            db,
            action=REVEAL_AUDIT_ACTION,
            entity_type="subscriber",
            entity_id=str(subscriber_id),
            actor=audit_actor,
            metadata={**metadata, "reason": "rate_limited", **actor_metadata},
            status_code=429,
            is_success=False,
        )
        return _json(
            {"password": "", "detail": "Reveal rate limit reached. Try again later."},
            429,
        )

    try:
        password, found = lookup()
    except CredentialUnusable as exc:
        record_audit_event(
            db,
            action=REVEAL_AUDIT_ACTION,
            entity_type="subscriber",
            entity_id=str(subscriber_id),
            actor=audit_actor,
            metadata={
                **metadata,
                "reason": exc.reason,
                "refused": True,
                **actor_metadata,
            },
            status_code=409,
            is_success=False,
        )
        return _json({"password": "", "detail": UNUSABLE_MESSAGE}, 409)
    record_audit_event(
        db,
        action=REVEAL_AUDIT_ACTION,
        entity_type="subscriber",
        entity_id=str(subscriber_id),
        actor=audit_actor,
        metadata={**metadata, "found": bool(found), **actor_metadata},
        status_code=200 if found else 404,
        is_success=bool(found),
    )
    if not found:
        return _json({"password": ""}, 404)
    return _json({"password": password}, 200)
