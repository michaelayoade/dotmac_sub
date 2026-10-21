"""Failure-side owner unit lane; file SQLite is not deployed-schema acceptance."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, insert, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.models.audit import AuditActorType, AuditEvent
from app.models.auth import AuthProvider, MFAMethod, UserCredential
from app.services import password_authentication as pa


@pytest.fixture
def denial_database(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'denials.sqlite'}")
    UserCredential.__table__.create(engine)
    MFAMethod.__table__.create(engine)
    AuditEvent.__table__.create(engine)

    @contextmanager
    def independent_session():
        with Session(engine) as db:
            yield db

    monkeypatch.setattr(
        pa.db_session_adapter, "owner_command_session", independent_session
    )
    credential_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            insert(UserCredential).values(
                id=credential_id,
                subscriber_id=uuid4(),
                provider=AuthProvider.local,
                username="synthetic-denial",
                password_hash="test-only-representation",
                credential_version=7,
                failed_login_attempts=0,
            )
        )
    yield engine, credential_id
    engine.dispose()


def _denial(credential_id, *, effect=True, request=None):
    return pa.denial_for_request(
        reason=pa.DenialReason.INVALID_CREDENTIALS,
        surface=pa.DenialSurface.LOGIN,
        phase=pa.DenialPhase.PASSWORD,
        target=pa.DenialTarget(pa.DenialTargetKind.CREDENTIAL, credential_id),
        request=request,
        effect=pa.PasswordFailureEffect(credential_id, pa.FailurePolicy(3, 15))
        if effect
        else None,
    )


def _facts(engine, credential_id):
    with Session(engine) as db:
        credential = db.get(UserCredential, credential_id)
        assert credential is not None
        rows = db.scalars(select(AuditEvent)).all()
        return (
            credential.failed_login_attempts,
            credential.credential_version,
            credential.last_login_at,
            credential.password_hash,
            rows,
        )


def test_counter_and_real_audit_row_commit_together(denial_database):
    engine, credential_id = denial_database
    pa.record_authentication_denial(_denial(credential_id))
    count, version, last_login, representation, rows = _facts(engine, credential_id)
    assert (count, version, last_login, representation) == (
        1,
        7,
        None,
        "test-only-representation",
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.actor_type is AuditActorType.system
    assert row.actor_id == pa.OWNER
    assert row.actor_party_id is None
    assert row.is_success is False
    assert row.status_code == 401
    assert row.entity_id == str(credential_id)
    assert row.action == "auth.authentication_denied"


@pytest.mark.parametrize("failure_point", ["stage", "flush", "commit", "open", "close"])
def test_failure_never_escapes_and_counter_is_not_committed_without_audit(
    denial_database,
    monkeypatch,
    failure_point,
    caplog,
):
    engine, credential_id = denial_database
    marker = "private-password-sql-parameter"

    def fail(*args, **kwargs):
        raise RuntimeError(marker)

    if failure_point == "stage":
        monkeypatch.setattr(pa, "stage_audit_event", fail)
    elif failure_point in {"flush", "commit"}:
        event.listen(
            Session,
            "before_flush" if failure_point == "flush" else "before_commit",
            fail,
        )
    elif failure_point == "open":
        monkeypatch.setattr(pa.db_session_adapter, "owner_command_session", fail)
    else:

        @contextmanager
        def close_failure():
            with Session(engine) as db:
                yield db
            raise RuntimeError(marker)

        monkeypatch.setattr(
            pa.db_session_adapter, "owner_command_session", close_failure
        )
    try:
        pa.record_authentication_denial(_denial(credential_id))
    finally:
        if failure_point in {"flush", "commit"}:
            event.remove(
                Session,
                "before_flush" if failure_point == "flush" else "before_commit",
                fail,
            )
    count, _, _, _, rows = _facts(engine, credential_id)
    assert (count, len(rows)) == ((1, 1) if failure_point == "close" else (0, 0))
    assert marker not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_audit_outage_does_not_remove_an_existing_lock(denial_database, monkeypatch):
    engine, credential_id = denial_database
    locked_until = datetime.now(UTC) + timedelta(minutes=20)
    with engine.begin() as connection:
        connection.execute(
            UserCredential.__table__.update()
            .values(
                locked_until=locked_until,
                failed_login_attempts=2,
            )
            .where(UserCredential.id == credential_id)
        )

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic audit unavailable")

    monkeypatch.setattr(pa, "stage_audit_event", fail)
    pa.record_authentication_denial(_denial(credential_id))
    with Session(engine) as db:
        row = db.get(UserCredential, credential_id)
        assert row.failed_login_attempts == 2
        assert row.locked_until.replace(tzinfo=UTC) == locked_until
        assert db.scalars(select(AuditEvent)).all() == []


def test_unknown_target_has_no_row_or_session(denial_database, monkeypatch):
    engine, credential_id = denial_database
    attempts: list[bool] = []

    def forbidden():
        attempts.append(True)
        raise AssertionError("unknown denial must not open a session")

    monkeypatch.setattr(pa.db_session_adapter, "owner_command_session", forbidden)
    denial = _denial(credential_id, effect=False)
    from dataclasses import replace

    pa.record_authentication_denial(replace(denial, target=None))
    assert attempts == []
    assert _facts(engine, credential_id)[0] == 0
    assert _facts(engine, credential_id)[4] == []


def test_request_metadata_is_bounded_and_normalized(denial_database):
    engine, credential_id = denial_database
    marker = "private-password-cookie-jwt-url"
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/" + marker,
            "client": ("192.0.2.9", 1234),
            "headers": [
                (b"x-request-id", marker.encode()),
                (b"cookie", marker.encode()),
                (b"user-agent", marker.encode()),
                (b"authorization", marker.encode()),
            ],
        }
    )
    denial = _denial(credential_id, request=request)
    assert denial.trusted_ip == ip_address("192.0.2.9")
    pa.record_authentication_denial(denial)
    rows = _facts(engine, credential_id)[4]
    assert marker not in str(rows[0].metadata_)
    assert rows[0].user_agent is None
    assert rows[0].request_id == str(denial.correlation_id)
    assert set(rows[0].metadata_) == {
        "schema_version",
        "reason",
        "surface",
        "phase",
        "target_id",
        "provenance",
        "attempt_id",
        "correlation_id",
    }


def test_request_pending_and_flushed_writes_are_never_completed(denial_database):
    engine, credential_id = denial_database
    with Session(engine) as request_session:
        # A pending request write remains pending after successful evidence.
        pending = AuditEvent(action="test.request.pending", entity_type="test")
        request_session.add(pending)
        pa.record_password_failure(
            request_session, credential_id, pa.FailurePolicy(3, 15)
        )
        assert pending in request_session.new
        assert _facts(engine, credential_id)[0] == 1
        request_session.flush()
        # File SQLite permits one writer: an independent failure attempt may
        # be unavailable here; it must not release/commit the existing writer.
        pa.record_authentication_denial(_denial(credential_id))
        assert request_session.in_transaction()
        assert request_session.get(AuditEvent, pending.id) is pending
        request_session.rollback()
    with Session(engine) as db:
        assert (
            db.scalar(
                select(AuditEvent).where(AuditEvent.action == "test.request.pending")
            )
            is None
        )


def test_telemetry_failure_cannot_escape(denial_database, monkeypatch):
    import app.metrics as metrics

    engine, credential_id = denial_database

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic telemetry unavailable")

    monkeypatch.setattr(pa.db_session_adapter, "owner_command_session", fail)
    monkeypatch.setattr(metrics.AUTHENTICATION_DENIAL_WRITE_FAILURES, "inc", fail)
    monkeypatch.setattr(pa.logger, "warning", fail)
    pa.record_authentication_denial(_denial(credential_id))
    assert _facts(engine, credential_id)[0] == 0


def test_external_source_snapshot_repr_excludes_identifier_and_secret():
    marker = "synthetic-private-source-material"
    query = pa.PortalAuthenticationSourceQuery(identifier=marker)
    snapshot = pa.PortalAccessSnapshot(uuid4(), None, marker, uuid4())
    assert marker not in repr(query)
    assert marker not in repr(snapshot)
