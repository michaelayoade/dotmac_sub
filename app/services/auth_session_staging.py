"""Flush-only authentication session issuance and memory-only token encoding.

Calling owners authenticate the exact principal, retain standing locks, stage
canonical audit evidence and complete the transaction. This participant owns
session insertion, device supersession and delegated staff presence only.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.auth import Session as AuthSession
from app.models.auth import SessionStatus
from app.services import staff_party_authentication, team_inbox_assignment
from app.services.domain_errors import DomainError

PrincipalType = Literal["system_user", "reseller_user", "subscriber"]


class SessionStagingError(DomainError):
    def __init__(self) -> None:
        super().__init__(
            code="auth.session_staging.invalid_command",
            message="A canonical authentication session command is required.",
            retryable=False,
        )


@dataclass(frozen=True, slots=True)
class SessionClientEvidence:
    ip_address: str | None
    user_agent: str | None
    device_id: str | None


@dataclass(frozen=True, slots=True)
class StageSessionCommand:
    principal_type: PrincipalType
    principal_id: UUID
    client: SessionClientEvidence
    staff_binding: staff_party_authentication.StaffSessionBinding | None = None
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class StagedSession:
    """A flushed, NOT committed session plus every input the tokens need.

    Everything a token needs is resolved while the staging transaction owns
    the session: reading settings after commit would start an implicit caller
    transaction and make the next owner command fail its entry guard. Tokens
    are encoded from this value only AFTER the owning transaction commits.
    """

    session_id: str
    principal_type: str
    principal_id: str
    refresh_token: str = field(repr=False)
    access_ttl_minutes: int
    secret: str = field(repr=False)
    algorithm: str
    issued_at: datetime


def stage_session_issue(db: Session, *, command: StageSessionCommand) -> StagedSession:
    """Stage a session in the calling owner's transaction; never complete it."""
    from app.services import auth_flow as policy

    if (
        command.principal_type not in {"system_user", "reseller_user", "subscriber"}
        or not isinstance(command.principal_id, UUID)
        or not isinstance(command.client, SessionClientEvidence)
    ):
        raise SessionStagingError()
    principal_type = command.principal_type
    principal_uuid = command.principal_id
    staff_binding = command.staff_binding
    expires_at = command.expires_at
    if principal_type == "system_user":
        if staff_binding is None:
            raise staff_party_authentication.StaffProjectionError(
                staff_party_authentication.StaffProjectionRefusal.projection_missing,
                principal_uuid,
            )
        # Validate the Party-keyed identity/context pair before revoking a
        # prior device session or performing any other write.
        staff_principal = staff_party_authentication.resolve_staff_principal_by_party(
            db,
            staff_binding.party_id,
            staff_binding.system_user_id,
            reference=principal_uuid,
        )
        if staff_principal.id != principal_uuid:
            raise staff_party_authentication.StaffProjectionError(
                staff_party_authentication.StaffProjectionRefusal.projection_conflict,
                principal_uuid,
            )
    refresh_token = secrets.token_urlsafe(48)
    now = policy._now()
    if expires_at is None:
        expires_at = now + timedelta(days=policy._refresh_ttl_days(db))
    # Resolve every signing input while this issuance transaction owns the
    # session. Reading settings after commit would start an implicit caller
    # transaction and make the next owner command fail its entry guard.
    access_ttl_minutes = policy._access_ttl_minutes(db)
    access_secret = policy._jwt_secret(db)
    access_algorithm = policy._jwt_algorithm(db)
    device_id = command.client.device_id
    session_kwargs = dict(
        status=SessionStatus.active,
        token_hash=policy._hash_token(refresh_token),
        ip_address=command.client.ip_address,
        user_agent=command.client.user_agent,
        device_id=device_id,
        created_at=now,
        last_seen_at=now,
        expires_at=expires_at,
    )
    principal_column = {
        "system_user": AuthSession.system_user_id,
        "reseller_user": AuthSession.reseller_user_id,
    }.get(principal_type, AuthSession.subscriber_id)
    # Per-device replace: a re-login from a known device supersedes that
    # device's prior active session instead of stacking a new row. Revoked in
    # the same transaction as the insert, so the principal always has at most
    # one active session per device.
    if device_id:
        db.query(AuthSession).filter(
            principal_column == principal_uuid,
            AuthSession.device_id == device_id,
            AuthSession.status == SessionStatus.active,
            AuthSession.revoked_at.is_(None),
        ).update(
            {
                AuthSession.status: SessionStatus.revoked,
                AuthSession.revoked_at: now,
            },
            synchronize_session=False,
        )
    if principal_type == "system_user":
        # Write BOTH halves of the bound pair. `party_id` is the identity
        # the later ratchet will validate from; `system_user_id` stays as
        # the Sub-owned staff context and is not being retired. The typed
        # binding was resolved from Party before any mutation above.
        assert staff_binding is not None
        session = AuthSession(
            system_user_id=principal_uuid,
            party_id=staff_binding.party_id,
            **session_kwargs,
        )
    elif principal_type == "reseller_user":
        session = AuthSession(reseller_user_id=principal_uuid, **session_kwargs)
    else:
        session = AuthSession(subscriber_id=principal_uuid, **session_kwargs)
    db.add(session)
    db.flush()
    if principal_type == "system_user":
        team_inbox_assignment.record_agent_signed_in_presence(
            db,
            command=team_inbox_assignment.AgentSignedInPresenceCommand(
                system_user_id=principal_uuid,
                auth_session_id=session.id,
                signed_in_at=now,
            ),
        )
    session_id = str(session.id)
    return StagedSession(
        session_id=session_id,
        principal_type=principal_type,
        principal_id=str(principal_uuid),
        refresh_token=refresh_token,
        access_ttl_minutes=access_ttl_minutes,
        secret=access_secret,
        algorithm=access_algorithm,
        issued_at=now,
    )


def tokens_for_staged_session(staged: StagedSession) -> dict[str, str]:
    """Encode the token pair for a session whose transaction has committed."""

    from app.services import auth_flow as policy

    access_token = policy._encode_access_token(
        principal_id=staged.principal_id,
        principal_type=staged.principal_type,
        session_id=staged.session_id,
        issued_at=staged.issued_at,
        ttl_minutes=staged.access_ttl_minutes,
        secret=staged.secret,
        algorithm=staged.algorithm,
    )
    return {"access_token": access_token, "refresh_token": staged.refresh_token}
