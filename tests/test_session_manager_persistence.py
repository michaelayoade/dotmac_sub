"""Independent SQLite root-transaction persistence regression tests."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.models.auth import Session as AuthSession
from app.models.auth import SessionStatus
from app.services import session_manager
from tests import oidc_atomic_unit
from tests.support.tx_trace import assert_protected_tx, trace_transactions

db_session = oidc_atomic_unit.db_session
oidc_unit_engine = oidc_atomic_unit.oidc_unit_engine


def _session(subscriber_id):
    return AuthSession(
        subscriber_id=subscriber_id,
        status=SessionStatus.active,
        token_hash=f"token-{uuid4()}",
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )


def test_revoke_session_commits_before_return(db_session, person):
    principal_id = person.id
    current, target = _session(principal_id), _session(principal_id)
    db_session.add_all([current, target])
    db_session.flush()
    current_id, target_id = current.id, target.id
    db_session.commit()
    with trace_transactions(db_session.get_bind().engine) as trace:
        session_manager.revoke_session(
            db_session,
            session_id=str(target_id),
            subscriber_id=principal_id,
            current_session_id=str(current_id),
        )
    assert_protected_tx(trace.result)
    db_session.expire_all()
    persisted = db_session.get(AuthSession, target_id)
    assert persisted.status == SessionStatus.revoked
    assert persisted.revoked_at is not None


def test_revoke_all_other_sessions_commits_before_return(db_session, person):
    principal_id = person.id
    current, other = _session(principal_id), _session(principal_id)
    db_session.add_all([current, other])
    db_session.flush()
    current_id, other_id = current.id, other.id
    db_session.commit()
    with trace_transactions(db_session.get_bind().engine) as trace:
        result = session_manager.revoke_all_other_sessions(
            db_session, subscriber_id=principal_id, current_session_id=str(current_id)
        )
    assert_protected_tx(trace.result)
    db_session.rollback()
    db_session.expire_all()
    assert result.revoked_count == 1
    assert db_session.get(AuthSession, current_id).status == SessionStatus.active
    persisted = db_session.get(AuthSession, other_id)
    assert persisted.status == SessionStatus.revoked
    assert persisted.revoked_at is not None
