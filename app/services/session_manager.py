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

logger = logging.getLogger(__name__)


def list_sessions(
    db: Session,
    subscriber_id: UUID,
    current_session_id: str | None,
) -> SessionListResponse:
    """List active sessions for a subscriber."""
    stmt = (
        select(AuthSession)
        .where(AuthSession.subscriber_id == subscriber_id)
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
) -> SessionRevokeResponse:
    """Revoke a single session belonging to the subscriber."""
    stmt = (
        select(AuthSession)
        .where(AuthSession.id == session_id)
        .where(AuthSession.subscriber_id == subscriber_id)
    )
    session = db.scalars(stmt).first()

    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if session.status == SessionStatus.revoked:
        raise HTTPException(status_code=400, detail="Session already revoked")

    now = datetime.now(UTC)
    session.status = SessionStatus.revoked
    session.revoked_at = now
    db.flush()

    return SessionRevokeResponse(revoked_at=now)


def revoke_all_other_sessions(
    db: Session,
    subscriber_id: UUID,
    current_session_id: str | None,
) -> SessionRevokeResponse:
    """Revoke all active sessions except the current one."""
    stmt = (
        select(AuthSession)
        .where(AuthSession.subscriber_id == subscriber_id)
        .where(AuthSession.status == SessionStatus.active)
        .where(AuthSession.revoked_at.is_(None))
        .where(AuthSession.id != current_session_id)
    )
    sessions = db.scalars(stmt).all()

    now = datetime.now(UTC)
    for s in sessions:
        s.status = SessionStatus.revoked
        s.revoked_at = now

    db.flush()

    return SessionRevokeResponse(revoked_at=now, revoked_count=len(sessions))
