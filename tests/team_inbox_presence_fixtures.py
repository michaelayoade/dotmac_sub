"""Explicit authenticated-session evidence for Team Inbox test agents."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.models.auth import Session as AuthSession
from app.models.auth import SessionStatus
from app.models.system_user import SystemUser
from app.models.team_inbox import InboxAgentPresenceSessionLease


def add_active_presence_lease(
    db: Session,
    *,
    system_user_id: UUID,
    observed_at: datetime | None = None,
) -> InboxAgentPresenceSessionLease:
    """Add the real auth-session evidence required by an online test presence."""

    seen_at = observed_at or datetime.now(UTC)
    system_user = db.get(SystemUser, system_user_id)
    if system_user is None:
        raise AssertionError("online presence fixture requires a persisted staff user")
    auth_session = AuthSession(
        system_user_id=system_user.id,
        party_id=system_user.person_party_id,
        status=SessionStatus.active,
        token_hash=f"team-inbox-presence-test:{uuid4()}",
        created_at=seen_at,
        last_seen_at=seen_at,
        expires_at=seen_at + timedelta(days=1),
    )
    db.add(auth_session)
    db.flush()
    lease = InboxAgentPresenceSessionLease(
        person_id=system_user.id,
        auth_session_id=auth_session.id,
        last_seen_at=seen_at,
    )
    db.add(lease)
    db.flush()
    return lease
