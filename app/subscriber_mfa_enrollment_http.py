"""Transport normalization and session lifecycle for subscriber enrollment."""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException

from app.services import password_authentication as owner
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext


def session_proof(
    *, principal_type: str, principal_id: str, session_id: str, is_impersonation: bool
) -> owner.SubscriberMfaSessionProof:
    if principal_type != "subscriber" or is_impersonation:
        raise HTTPException(
            status_code=403, detail="Authenticated subscriber session required"
        )
    try:
        return owner.SubscriberMfaSessionProof(
            UUID(session_id), UUID(principal_id), False
        )
    except (TypeError, ValueError, AttributeError) as exc:
        raise HTTPException(
            status_code=401, detail="Authenticated subscriber session required"
        ) from exc


def command_context(
    *, proof: owner.SubscriberMfaSessionProof, reason: str
) -> CommandContext:
    return CommandContext.system(
        actor=f"subscriber:{proof.subscriber_id}",
        scope=owner.SUBSCRIBER_MFA_SCOPE,
        reason=reason,
    )


def http_error(exc: owner.PasswordAuthenticationError) -> HTTPException:
    status_code = {
        "invalid_mfa_session": 401,
        "invalid_mfa_method": 404,
        "invalid_mfa_code": 401,
        "mfa_locked": 429,
        "account_disabled": 403,
        "lock_timeout": 503,
        "mfa_unavailable": 503,
    }.get(exc.kind, 400)
    return HTTPException(
        status_code=status_code,
        detail=exc.message,
        headers={"Retry-After": "5"} if status_code == 503 else None,
    )


def setup(
    *, command: owner.SetupSubscriberMfaCommand
) -> owner.SubscriberMfaSetupOutcome:
    try:
        with db_session_adapter.owner_command_session() as db:
            return owner.setup_subscriber_mfa(db, command=command)
    except owner.PasswordAuthenticationError as exc:
        raise http_error(exc) from exc


def confirm(
    *, command: owner.ConfirmSubscriberMfaCommand
) -> owner.SubscriberMfaConfirmationOutcome:
    try:
        with db_session_adapter.owner_command_session() as db:
            return owner.confirm_subscriber_mfa(db, command=command)
    except owner.PasswordAuthenticationError as exc:
        raise http_error(exc) from exc
