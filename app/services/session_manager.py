"""Service layer for session list/revoke endpoints."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.auth import Session as AuthSession
from app.models.auth import SessionStatus
from app.schemas.auth_flow import (
    SessionInfoResponse,
    SessionListResponse,
    SessionRevokeResponse,
)
from app.services import auth_cache

logger = logging.getLogger(__name__)


def _principal_filter(subscriber_id: UUID, principal_type: str):
    """Sessions are keyed by subscriber_id OR system_user_id depending on who
    is logged in; filtering on the wrong column silently returns nothing."""
    if principal_type == "system_user":
        return AuthSession.system_user_id == subscriber_id
    return AuthSession.subscriber_id == subscriber_id


def list_sessions(
    db: Session,
    subscriber_id: UUID,
    current_session_id: str | None,
    principal_type: str = "subscriber",
) -> SessionListResponse:
    """List active sessions for a principal."""
    stmt = (
        select(AuthSession)
        .where(_principal_filter(subscriber_id, principal_type))
        .where(AuthSession.status == SessionStatus.active)
        .where(AuthSession.revoked_at.is_(None))
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


def revoke_session(
    db: Session,
    session_id: str,
    subscriber_id: UUID,
    principal_type: str = "subscriber",
) -> SessionRevokeResponse:
    """Revoke a single session belonging to the principal."""
    stmt = (
        select(AuthSession)
        .where(AuthSession.id == session_id)
        .where(_principal_filter(subscriber_id, principal_type))
    )
    session = db.scalars(stmt).first()

    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if session.status == SessionStatus.revoked:
        raise HTTPException(status_code=400, detail="Session already revoked")

    now = datetime.now(UTC)
    principal_type = "system_user" if session.system_user_id else "subscriber"
    principal_id = str(session.system_user_id or session.subscriber_id)
    session.status = SessionStatus.revoked
    session.revoked_at = now
    db.flush()
    auth_cache.invalidate_session_context(
        str(session.id),
        principal_type=principal_type,
        principal_id=principal_id,
    )

    return SessionRevokeResponse(revoked_at=now)


def revoke_all_other_sessions(
    db: Session,
    subscriber_id: UUID,
    current_session_id: str | None,
    principal_type: str = "subscriber",
) -> SessionRevokeResponse:
    """Revoke all active sessions except the current one."""
    stmt = (
        select(AuthSession)
        .where(_principal_filter(subscriber_id, principal_type))
        .where(AuthSession.status == SessionStatus.active)
        .where(AuthSession.revoked_at.is_(None))
        .where(AuthSession.id != current_session_id)
    )
    sessions = db.scalars(stmt).all()

    now = datetime.now(UTC)
    for s in sessions:
        principal_type = "system_user" if s.system_user_id else "subscriber"
        principal_id = str(s.system_user_id or s.subscriber_id)
        s.status = SessionStatus.revoked
        s.revoked_at = now
        auth_cache.invalidate_session_context(
            str(s.id),
            principal_type=principal_type,
            principal_id=principal_id,
        )

    db.flush()

    return SessionRevokeResponse(revoked_at=now, revoked_count=len(sessions))
