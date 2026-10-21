"""Service layer for session list/revoke endpoints."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import and_, or_, select, text
from sqlalchemy.orm import Session

from app.models.auth import Session as AuthSession
from app.models.auth import SessionStatus
from app.schemas.auth_flow import (
    SessionInfoResponse,
    SessionListResponse,
    SessionRevokeResponse,
)
from app.services.audit_adapter import AuditActor, stage_audit_event
from app.services.auth_session_refresh import SessionPrincipalType, hash_refresh_token
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

logger = logging.getLogger(__name__)


def _principal_filter(subscriber_id: UUID, principal_type: str):
    """Sessions are keyed by subscriber_id OR system_user_id depending on who
    is logged in; filtering on the wrong column silently returns nothing."""
    columns = {
        "system_user": AuthSession.system_user_id,
        "subscriber": AuthSession.subscriber_id,
        "reseller_user": AuthSession.reseller_user_id,
    }
    selected = columns[principal_type]
    return and_(
        selected == subscriber_id,
        *(
            column.is_(None)
            for name, column in columns.items()
            if name != principal_type
        ),
    )


def list_sessions(
    db: Session,
    subscriber_id: UUID,
    current_session_id: str | None,
    principal_type: str = "subscriber",
) -> SessionListResponse:
    """List active sessions for a principal."""
    # Only genuinely-live sessions: active, not revoked, AND not past refresh
    # expiry. Expired-but-not-yet-cleaned rows were still listed, which is the
    # main reason the "Active sessions" screen accumulated stale entries.
    stmt = (
        select(AuthSession)
        .where(_principal_filter(subscriber_id, principal_type))
        .where(AuthSession.status == SessionStatus.active)
        .where(AuthSession.revoked_at.is_(None))
        .where(AuthSession.expires_at > datetime.now(UTC))
        .order_by(AuthSession.created_at.desc())
    )
    sessions = db.scalars(stmt).all()

    return SessionListResponse(
        sessions=[
            SessionInfoResponse(
                id=s.id,
                status=s.status.value,
                ip_address=s.ip_address,
                user_agent=s.user_agent,
                created_at=s.created_at,
                last_seen_at=s.last_seen_at,
                expires_at=s.expires_at,
                is_current=(str(s.id) == current_session_id),
            )
            for s in sessions
        ],
        total=len(sessions),
    )


OWNER = "app_sessions.auth"
CONCERN = "database auth-session revocation"
_DEFINITION = OwnerCommandDefinition(
    owner=OWNER, concern=CONCERN, name="revoke_authentication_sessions"
)


class RevocationIntent(StrEnum):
    LOGOUT = "logout"
    SINGLE = "single"
    OTHERS = "others"
    ADMIN = "admin"
    COMPENSATE = "vendor_admission_compensation"


class RevocationProvenance(StrEnum):
    CURRENT_REFRESH_BEARER = "current_refresh_bearer"
    AUTHENTICATED_USER_SESSION = "authenticated_user_session"
    AUTHENTICATED_ADMINISTRATOR = "authenticated_administrator"
    VENDOR_ADMISSION_REFUSAL = "vendor_admission_refusal"


class SessionRevocationError(DomainError):
    """Transport-neutral refusal of an ambiguous revocation."""


@dataclass(frozen=True, slots=True)
class RevokeSessionsCommand:
    context: CommandContext
    intent: RevocationIntent
    actor: AuditActor | None = None
    principal_type: SessionPrincipalType | None = None
    principal_id: UUID | None = None
    session_id: UUID | None = None
    current_session_id: UUID | None = None
    refresh_token: str | None = field(default=None, repr=False)
    provenance: RevocationProvenance = field(init=False)

    def __post_init__(self) -> None:
        sources = {
            RevocationIntent.LOGOUT: RevocationProvenance.CURRENT_REFRESH_BEARER,
            RevocationIntent.SINGLE: RevocationProvenance.AUTHENTICATED_USER_SESSION,
            RevocationIntent.OTHERS: RevocationProvenance.AUTHENTICATED_USER_SESSION,
            RevocationIntent.ADMIN: RevocationProvenance.AUTHENTICATED_ADMINISTRATOR,
            RevocationIntent.COMPENSATE: RevocationProvenance.VENDOR_ADMISSION_REFUSAL,
        }
        object.__setattr__(self, "provenance", sources[self.intent])


class RevocationDisposition(StrEnum):
    REVOKED = "revoked"
    NOOP = "noop"


@dataclass(frozen=True, slots=True)
class SessionRevocationOutcome:
    principal_type: SessionPrincipalType
    principal_id: UUID
    revoked_ids: tuple[UUID, ...]
    revoked_at: datetime | None
    disposition: RevocationDisposition


def _refuse(suffix: str, message: str) -> SessionRevocationError:
    return SessionRevocationError(code=f"{OWNER}.{suffix}", message=message)


def _binding(row: AuthSession) -> tuple[SessionPrincipalType, UUID]:
    bindings = [
        (kind, value)
        for kind, value in (
            (SessionPrincipalType.SUBSCRIBER, row.subscriber_id),
            (SessionPrincipalType.SYSTEM_USER, row.system_user_id),
            (SessionPrincipalType.RESELLER_USER, row.reseller_user_id),
        )
        if value is not None
    ]
    if len(bindings) != 1:
        raise _refuse("invalid_principal", "Session principal binding is ambiguous.")
    return bindings[0]


def revoke_authentication_sessions(
    db: Session, command: RevokeSessionsCommand
) -> SessionRevocationOutcome:
    """Commit the decision, audit and exact-target projection intent together."""

    def operation() -> SessionRevocationOutcome:
        from app.models.subscriber import ResellerUser, Subscriber
        from app.models.system_user import SystemUser

        if db.get_bind().dialect.name == "postgresql":
            db.execute(text("SELECT set_config('lock_timeout', '5s', true)"))
        bearer = command.intent in {
            RevocationIntent.LOGOUT,
            RevocationIntent.COMPENSATE,
        }
        discovered = None
        target_id: UUID | None
        if bearer:
            if not command.refresh_token:
                raise _refuse("not_found", "Session not found.")
            discovered = db.scalars(
                select(AuthSession).where(
                    AuthSession.token_hash == hash_refresh_token(command.refresh_token)
                )
            ).first()
        elif command.intent is RevocationIntent.ADMIN:
            if command.actor is None:
                raise _refuse(
                    "invalid_actor",
                    "An authenticated administrative actor is required.",
                )
            discovered = db.get(AuthSession, command.session_id)
        if bearer or command.intent is RevocationIntent.ADMIN:
            if discovered is None:
                raise _refuse("not_found", "Session not found.")
            kind, principal_id = _binding(discovered)
            target_id = discovered.id
        else:
            if (
                command.principal_type is None
                or command.principal_id is None
                or command.current_session_id is None
            ):
                raise _refuse(
                    "invalid_current_session",
                    "A current authenticated session is required.",
                )
            kind, principal_id = command.principal_type, command.principal_id
            target_id = command.session_id
        principal_pk = {
            SessionPrincipalType.SUBSCRIBER: Subscriber.id,
            SessionPrincipalType.SYSTEM_USER: SystemUser.id,
            SessionPrincipalType.RESELLER_USER: ResellerUser.id,
        }[kind]
        principal = db.scalars(
            select(principal_pk)
            .where(principal_pk == principal_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()
        if principal is None:
            raise _refuse("invalid_principal", "Session principal is unavailable.")
        binding = _principal_filter(principal_id, kind.value)
        if command.intent in {RevocationIntent.SINGLE, RevocationIntent.OTHERS}:
            if (
                command.actor is None
                or command.actor.actor_type.value != "user"
                or command.actor.actor_id != str(principal_id)
            ):
                raise _refuse(
                    "invalid_actor", "Self revocation requires the authenticated user."
                )
        statement = select(AuthSession).where(binding)
        if command.intent is RevocationIntent.OTHERS:
            statement = statement.where(
                or_(
                    AuthSession.id == command.current_session_id,
                    and_(
                        AuthSession.status == SessionStatus.active,
                        AuthSession.revoked_at.is_(None),
                    ),
                )
            )
        elif command.intent is RevocationIntent.SINGLE:
            statement = statement.where(
                AuthSession.id.in_([target_id, command.current_session_id])
            )
        else:
            statement = statement.where(AuthSession.id == target_id)
        rows = list(
            db.scalars(
                statement.order_by(AuthSession.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        if command.intent in {RevocationIntent.SINGLE, RevocationIntent.OTHERS}:
            current = next(
                (row for row in rows if row.id == command.current_session_id), None
            )
            if (
                current is None
                or current.status != SessionStatus.active
                or current.revoked_at is not None
                or _utc(current.expires_at) <= datetime.now(UTC)
            ):
                raise _refuse(
                    "invalid_current_session",
                    "A current authenticated session is required.",
                )
            rows = [
                row
                for row in rows
                if (
                    row.id != command.current_session_id
                    if command.intent is RevocationIntent.OTHERS
                    else row.id == target_id
                )
            ]
        if command.intent is not RevocationIntent.OTHERS and not rows:
            raise _refuse("not_found", "Session not found.")
        if bearer and (
            rows[0].token_hash != hash_refresh_token(command.refresh_token or "")
            or rows[0].revoked_at is not None
            or rows[0].status is SessionStatus.revoked
        ):
            raise _refuse("not_found", "Session not found.")
        now = datetime.now(UTC)
        changed = []
        for row in rows:
            if row.status is SessionStatus.revoked or row.revoked_at is not None:
                continue
            row.status = SessionStatus.revoked
            row.revoked_at = now
            changed.append(row.id)
        actor = command.actor
        if command.intent is RevocationIntent.LOGOUT:
            actor = AuditActor.user(str(principal_id))
        elif command.intent is RevocationIntent.COMPENSATE:
            actor = AuditActor.system("vendor_admission")
        if actor is None:
            raise _refuse("invalid_actor", "Revocation requires an accountable actor.")
        evidence = {
            "schema_version": 1,
            "command_id": str(command.context.command_id),
            "correlation_id": str(command.context.correlation_id),
            "intent": command.intent.value,
            "provenance": command.provenance.value,
            "disposition": RevocationDisposition.REVOKED.value
            if changed
            else RevocationDisposition.NOOP.value,
            "principal_type": kind.value,
            "principal_id": str(principal_id),
            "session_ids": [str(value) for value in changed],
        }
        stage_audit_event(
            db,
            action="auth.sessions_revoked"
            if changed
            else "auth.sessions_revocation_noop",
            entity_type="authentication_session",
            entity_id=str(target_id) if target_id else str(principal_id),
            actor=actor,
            metadata=evidence,
        )
        if changed:
            emit_event(
                db,
                EventType.authentication_sessions_revoked,
                {
                    **evidence,
                    "aggregate_type": "authentication_session_revocation",
                    "aggregate_id": str(command.context.command_id),
                    "aggregate_version": str(command.context.command_id),
                },
                actor=command.context.actor,
                dispatch_after_commit=False,
            )
        return SessionRevocationOutcome(
            principal_type=kind,
            principal_id=principal_id,
            revoked_ids=tuple(changed),
            revoked_at=now if changed else rows[0].revoked_at if rows else None,
            disposition=RevocationDisposition.REVOKED
            if changed
            else RevocationDisposition.NOOP,
        )

    return execute_owner_command(
        db, definition=_DEFINITION, context=command.context, operation=operation
    )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def revoke_session(
    db: Session,
    session_id: str,
    subscriber_id: UUID,
    principal_type: str = "subscriber",
    *,
    current_session_id: str | None = None,
) -> SessionRevokeResponse:
    outcome = revoke_authentication_sessions(
        db,
        RevokeSessionsCommand(
            context=CommandContext.system(
                actor=str(subscriber_id),
                scope="self_sessions",
                reason="single_session_revocation",
            ),
            intent=RevocationIntent.SINGLE,
            actor=AuditActor.user(str(subscriber_id)),
            principal_type=SessionPrincipalType(principal_type),
            principal_id=UUID(str(subscriber_id)),
            session_id=target_session_uuid(session_id),
            current_session_id=_current_uuid(current_session_id),
        ),
    )
    return SessionRevokeResponse(
        revoked_at=outcome.revoked_at, revoked_count=len(outcome.revoked_ids)
    )


def revoke_all_other_sessions(
    db: Session,
    subscriber_id: UUID,
    current_session_id: str | None,
    principal_type: str = "subscriber",
) -> SessionRevokeResponse:
    outcome = revoke_authentication_sessions(
        db,
        RevokeSessionsCommand(
            context=CommandContext.system(
                actor=str(subscriber_id),
                scope="self_sessions",
                reason="other_session_revocation",
            ),
            intent=RevocationIntent.OTHERS,
            actor=AuditActor.user(str(subscriber_id)),
            principal_type=SessionPrincipalType(principal_type),
            principal_id=UUID(str(subscriber_id)),
            current_session_id=_current_uuid(current_session_id),
        ),
    )
    return SessionRevokeResponse(
        revoked_at=outcome.revoked_at, revoked_count=len(outcome.revoked_ids)
    )


def _current_uuid(value: str | None) -> UUID | None:
    try:
        return UUID(value) if value else None
    except ValueError as exc:
        raise _refuse(
            "invalid_current_session", "A current authenticated session is required."
        ) from exc


def target_session_uuid(value: str) -> UUID:
    """Parse an adapter target without allowing malformed input to become 500."""
    try:
        return UUID(value)
    except ValueError as exc:
        raise _refuse("not_found", "Session not found.") from exc
