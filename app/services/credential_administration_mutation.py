"""Flush-only credential participant of administrative owner commands."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.auth import AuthProvider, SessionStatus, UserCredential
from app.models.auth import Session as AuthSession
from app.models.radius import RadiusServer
from app.services.audit_adapter import AuditActor, stage_audit_event
from app.services.credential_administration import (
    CreateCredential,
    CredentialField,
    CredentialOutcome,
    CredentialView,
    DeactivateCredential,
    UpdateCredential,
    _principal,
    error,
)
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import CommandContext, owner_command_active


def _validate(
    db: Session, provider: AuthProvider, username: str | None, server_id: UUID | None
) -> None:
    if provider == AuthProvider.local and not (username and username.strip()):
        raise error("invalid_username", "Local credentials require a username.")
    if server_id is not None and db.get(RadiusServer, server_id) is None:
        raise error("radius_not_found", "Radius server was not found.")


def _complete(
    db: Session,
    row: UserCredential,
    context: CommandContext,
    transition: str,
    changed: bool,
    revoke: bool,
) -> CredentialOutcome:
    kind, principal_id = _principal(row)
    count = 0
    if revoke:
        count = (
            db.query(AuthSession)
            .filter(
                getattr(AuthSession, f"{kind.value}_id") == principal_id,
                AuthSession.status == SessionStatus.active,
            )
            .update(
                {"status": SessionStatus.revoked, "revoked_at": datetime.now(UTC)},
                synchronize_session=False,
            )
        )
    db.flush()
    if changed:
        evidence = {
            "schema_version": 1,
            "credential_id": str(row.id),
            "aggregate_type": "user_credential",
            "aggregate_id": str(row.id),
            "aggregate_version": int(row.credential_version),
            "principal_type": kind.value,
            "principal_id": str(principal_id),
            "credential_version": row.credential_version,
            "transition": transition,
            "invalidate_sessions": revoke,
            "sessions_revoked": count,
            "command_id": str(context.command_id),
            "correlation_id": str(context.correlation_id),
            "causation_id": str(context.causation_id) if context.causation_id else None,
            "reason": context.reason,
        }
        actor = (
            AuditActor.service(context.actor[8:])
            if context.actor.startswith("service:")
            else AuditActor.user(context.actor)
        )
        stage_audit_event(
            db,
            action=f"auth.credential_admin.{transition}",
            entity_type="user_credential",
            entity_id=str(row.id),
            actor=actor,
            metadata=evidence,
        )
        emit_event(
            db,
            EventType.credential_administration_completed,
            evidence,
            actor=context.actor,
            subscriber_id=row.subscriber_id,
        )
    return CredentialOutcome(
        CredentialView.of(row),
        int(row.credential_version),
        int(count),
        changed,
        context.command_id,
        context.correlation_id,
    )


def create(
    db: Session,
    *,
    command: CreateCredential,
    provider: AuthProvider,
    password_hash: str | None,
) -> CredentialOutcome:
    if not owner_command_active(db, owner="auth.credential_administration"):
        raise error(
            "invalid_command_context",
            "Credential participant requires its administrative coordinator.",
        )
    _validate(db, provider, command.username, command.radius_server_id)
    row = UserCredential(
        subscriber_id=command.subscriber_id,
        provider=provider,
        username=command.username,
        radius_server_id=command.radius_server_id,
        password_hash=password_hash,
        credential_version=1,
        password_updated_at=(
            datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=1)
        )
        if password_hash
        else None,
        must_change_password=False,
        failed_login_attempts=0,
        is_active=True,
    )
    db.add(row)
    return _complete(db, row, command.context, "created", True, False)


def update(
    db: Session,
    *,
    command: UpdateCredential,
    row: UserCredential,
    password_hash: str | None,
) -> CredentialOutcome:
    if not owner_command_active(db, owner="auth.credential_administration"):
        raise error(
            "invalid_command_context",
            "Credential participant requires its administrative coordinator.",
        )
    fields = command.submitted_fields
    username = command.username if CredentialField.username in fields else row.username
    server = (
        command.radius_server_id
        if CredentialField.radius_server_id in fields
        else row.radius_server_id
    )
    _validate(db, row.provider, username, server)
    security_changed = (
        username != row.username
        or CredentialField.password in fields
        or (row.provider == AuthProvider.radius and server != row.radius_server_id)
    )
    changed = security_changed or server != row.radius_server_id
    row.username = username
    row.radius_server_id = server
    if CredentialField.password in fields:
        row.password_hash = password_hash
        # Reset JWT iat is compared at whole-second resolution. Invalidate
        # capabilities minted in the current second as well as older ones.
        row.password_updated_at = datetime.now(UTC).replace(microsecond=0) + timedelta(
            seconds=1
        )
        row.must_change_password = False
        row.failed_login_attempts = 0
        row.locked_until = None
    if security_changed:
        row.credential_version = int(row.credential_version or 1) + 1
    return _complete(db, row, command.context, "updated", changed, security_changed)


def deactivate(
    db: Session, *, command: DeactivateCredential, row: UserCredential
) -> CredentialOutcome:
    if not owner_command_active(db, owner="auth.credential_administration"):
        raise error(
            "invalid_command_context",
            "Credential participant requires its administrative coordinator.",
        )
    changed = row.is_active
    if changed:
        row.is_active = False
        row.credential_version = int(row.credential_version or 1) + 1
    return _complete(db, row, command.context, "deactivated", changed, changed)
