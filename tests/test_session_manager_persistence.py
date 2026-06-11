from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.models.auth import Session as AuthSession
from app.models.auth import SessionStatus
from app.services import session_manager


def _session(subscriber_id):
    return AuthSession(
        subscriber_id=subscriber_id,
        status=SessionStatus.active,
        token_hash=f"token-{uuid4()}",
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )


def test_revoke_session_commits_before_return(db_session, person):
    session = _session(person.id)
    db_session.add(session)
    db_session.commit()

    session_manager.revoke_session(db_session, str(session.id), person.id)
    db_session.rollback()
    db_session.expire_all()

    persisted = db_session.get(AuthSession, session.id)
    assert persisted.status == SessionStatus.revoked
    assert persisted.revoked_at is not None


def test_revoke_all_other_sessions_commits_before_return(db_session, person):
    current = _session(person.id)
    other = _session(person.id)
    db_session.add_all([current, other])
    db_session.commit()

    result = session_manager.revoke_all_other_sessions(
        db_session, person.id, str(current.id)
    )
    db_session.rollback()
    db_session.expire_all()

    assert result.revoked_count == 1
    assert db_session.get(AuthSession, current.id).status == SessionStatus.active
    persisted_other = db_session.get(AuthSession, other.id)
    assert persisted_other.status == SessionStatus.revoked
    assert persisted_other.revoked_at is not None
