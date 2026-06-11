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


def _count_commits(db_session, monkeypatch):
    """Spy on db.commit() so a test can prove the service persisted (committed),
    not just flushed, its change. The previous approach drove a real
    commit-then-rollback against the rollback-isolated fixture session, which
    detached the row (ObjectDeletedError); counting commits is equivalent for
    "commits before return" and doesn't fight the transactional fixture."""
    calls = {"n": 0}
    real_commit = db_session.commit

    def _spy():
        calls["n"] += 1
        return real_commit()

    monkeypatch.setattr(db_session, "commit", _spy)
    return calls


def test_revoke_session_commits_before_return(db_session, person, monkeypatch):
    session = _session(person.id)
    db_session.add(session)
    db_session.commit()

    commits = _count_commits(db_session, monkeypatch)
    session_manager.revoke_session(db_session, str(session.id), person.id)

    assert commits["n"] >= 1  # the service committed, not just flushed
    persisted = db_session.get(AuthSession, session.id)
    assert persisted.status == SessionStatus.revoked
    assert persisted.revoked_at is not None


def test_revoke_all_other_sessions_commits_before_return(
    db_session, person, monkeypatch
):
    current = _session(person.id)
    other = _session(person.id)
    db_session.add_all([current, other])
    db_session.commit()

    commits = _count_commits(db_session, monkeypatch)
    result = session_manager.revoke_all_other_sessions(
        db_session, person.id, str(current.id)
    )

    assert commits["n"] >= 1
    assert result.revoked_count == 1
    db_session.expire_all()
    assert db_session.get(AuthSession, current.id).status == SessionStatus.active
    persisted_other = db_session.get(AuthSession, other.id)
    assert persisted_other.status == SessionStatus.revoked
    assert persisted_other.revoked_at is not None
