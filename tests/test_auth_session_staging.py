"""Fast tests of the typed flush-only session participant boundary."""

from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.models.auth import Session as AuthSession
from app.models.auth import SessionStatus
from app.models.team_inbox import InboxAgentPresence
from app.services import auth_session_staging as staging
from app.services import staff_party_authentication as staff
from tests.oidc_mobile_fixtures import bind_field_technician, install_oidc_binding


def test_typed_staging_flushes_and_device_replacement_rolls_back_with_caller(
    db_session: Session,
) -> None:
    binding = install_oidc_binding(db_session)
    user, person, _ = bind_field_technician(db_session, binding)
    principal_id, party_id = user.id, person.id
    prior = AuthSession(
        system_user_id=principal_id,
        party_id=party_id,
        token_hash="test-staging-prior-hash",
        device_id="typed-device",
        status=SessionStatus.active,
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    db_session.add(prior)
    db_session.flush()
    prior_id = prior.id
    db_session.commit()
    staged = staging.stage_session_issue(
        db_session,
        command=staging.StageSessionCommand(
            principal_type="system_user",
            principal_id=principal_id,
            client=staging.SessionClientEvidence(
                "127.0.0.1", "typed-test", "typed-device"
            ),
            staff_binding=staff.StaffSessionBinding(
                party_id=party_id, system_user_id=principal_id
            ),
        ),
    )
    assert db_session.in_transaction()
    assert db_session.get(AuthSession, UUID(staged.session_id)) is not None
    db_session.refresh(prior)
    assert prior.status is SessionStatus.revoked and prior.revoked_at is not None
    assert db_session.query(InboxAgentPresence).one().status == "online"
    db_session.rollback()
    assert db_session.get(AuthSession, UUID(staged.session_id)) is None
    prior = db_session.get(AuthSession, prior_id)
    assert prior.status is SessionStatus.active and prior.revoked_at is None
    assert db_session.query(InboxAgentPresence).count() == 0


def test_invalid_typed_identifier_is_refused_before_any_session_mutation(
    db_session: Session,
) -> None:
    with pytest.raises(staging.SessionStagingError):
        staging.stage_session_issue(
            db_session,
            command=staging.StageSessionCommand(
                principal_type="subscriber",
                principal_id=cast(UUID, "not-a-uuid"),
                client=staging.SessionClientEvidence(None, None, "typed-device"),
            ),
        )
    assert not db_session.in_transaction()
    assert db_session.query(AuthSession).count() == 0


def test_encoding_staged_snapshot_performs_no_sql_after_commit(
    db_session: Session,
) -> None:
    binding = install_oidc_binding(db_session)
    user, person, _ = bind_field_technician(db_session, binding)
    principal_id, party_id = user.id, person.id
    db_session.commit()
    staged = staging.stage_session_issue(
        db_session,
        command=staging.StageSessionCommand(
            principal_type="system_user",
            principal_id=principal_id,
            client=staging.SessionClientEvidence(None, "typed-test", None),
            staff_binding=staff.StaffSessionBinding(
                party_id=party_id, system_user_id=principal_id
            ),
        ),
    )
    db_session.commit()
    engine = db_session.get_bind()

    def forbid_sql(*args, **kwargs):
        raise AssertionError("token encoding attempted database IO")

    event.listen(engine, "before_cursor_execute", forbid_sql)
    try:
        tokens = staging.tokens_for_staged_session(staged)
    finally:
        event.remove(engine, "before_cursor_execute", forbid_sql)
    assert tokens["access_token"] and tokens["refresh_token"]
    assert not db_session.in_transaction()
