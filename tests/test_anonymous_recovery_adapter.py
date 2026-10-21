"""Fast unit evidence for anonymous failure shape and owner rollback."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, event, select
from sqlalchemy.exc import StatementError
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.api.auth_flow import forgot_password
from app.models.audit import AuditEvent
from app.models.auth import Session as AuthSession
from app.models.event_store import EventStore
from app.models.notification import CommunicationIntentRecord, Notification
from app.schemas.auth_flow import ForgotPasswordRequest
from app.services import (
    anonymous_recovery_adapter,
    credential_recovery,
    web_auth,
    web_customer_auth,
    web_reseller_auth,
)
from app.services import (
    db_session_adapter as session_module,
)
from tests.test_credential_recovery import _context, _credential

_PRIVATE = "malicious@example.com SQL PARAM bearer HEADER\nforged-log"


def _command() -> credential_recovery.RequestPasswordRecoveryCommand:
    return credential_recovery.RequestPasswordRecoveryCommand(
        context=_context(), email=_PRIVATE
    )


@pytest.mark.parametrize("stage", ["audit", "outbox", "commit"])
def test_real_owner_rolls_back_and_api_shape_matches_unknown(
    db_session: Session, subscriber, monkeypatch: pytest.MonkeyPatch, caplog, stage: str
) -> None:
    email = "known-recovery@example.com"
    credential = _credential(db_session, subscriber, email=email)
    before_hash = credential.password_hash
    db_session.commit()
    owner_sessions: list[Session] = []

    def create_session() -> Session:
        session = Session(
            bind=db_session.get_bind(), join_transaction_mode="create_savepoint"
        )
        owner_sessions.append(session)
        if stage == "commit":

            def fail_commit(_session: Session) -> None:
                raise StatementError(
                    "unsafe SQL", "statement", {"email": _PRIVATE}, None
                )

            event.listen(session, "before_commit", fail_commit)
        return session

    monkeypatch.setattr(session_module, "SessionLocal", create_session)
    target = "stage_audit_event" if stage == "audit" else "emit_event"
    if stage != "commit":
        original = getattr(credential_recovery, target)

        def fail_after_stage(*args, **kwargs) -> None:
            original(*args, **kwargs)
            raise StatementError("unsafe SQL", "statement", {"email": _PRIVATE}, None)

        monkeypatch.setattr(credential_recovery, target, fail_after_stage)

    app = FastAPI()

    @app.post("/forgot-password")
    def endpoint(payload: ForgotPasswordRequest):
        return forgot_password(payload=payload, db=db_session)

    client = TestClient(app)
    unknown = client.post("/forgot-password", json={"email": "unknown@example.com"})
    known = client.post(
        "/forgot-password",
        json={"email": email},
        headers={"X-Private": _PRIVATE.replace("\n", "")},
    )
    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json()
    assert all(not session.in_transaction() for session in owner_sessions)
    assert (
        db_session.scalar(
            select(type(credential).password_hash).where(
                type(credential).id == credential.id
            )
        )
        == before_hash
    )
    for model in (
        AuditEvent,
        EventStore,
        Notification,
        CommunicationIntentRecord,
        AuthSession,
    ):
        assert db_session.query(model).count() == 0
    assert _PRIVATE not in caplog.text
    assert email not in caplog.text
    failures = [
        record
        for record in caplog.records
        if record.name == anonymous_recovery_adapter.__name__
    ]
    assert failures
    assert all(
        record.msg == "anonymous_password_recovery_failed"
        and not record.args
        and not record.exc_info
        for record in failures
    )


@pytest.mark.parametrize("failure", ["open", "owner", "close"])
@pytest.mark.parametrize("telemetry_fails", [False, True])
def test_lifecycle_and_telemetry_failures_remain_accepted(
    monkeypatch: pytest.MonkeyPatch, failure: str, telemetry_fails: bool
) -> None:
    @contextmanager
    def lifecycle() -> Generator[object, None, None]:
        if failure == "open":
            raise RuntimeError(_PRIVATE)
        yield object()
        if failure == "close":
            raise RuntimeError(_PRIVATE)

    monkeypatch.setattr(
        anonymous_recovery_adapter.db_session_adapter,
        "owner_command_session",
        lifecycle,
    )
    owner = Mock(side_effect=RuntimeError(_PRIVATE) if failure == "owner" else None)
    monkeypatch.setattr(credential_recovery, "request_password_recovery", owner)
    metric = Mock()
    logger = Mock()
    if telemetry_fails:
        metric.inc.side_effect = RuntimeError(_PRIVATE)
        logger.warning.side_effect = RuntimeError(_PRIVATE)
    monkeypatch.setattr(
        anonymous_recovery_adapter, "ANONYMOUS_RECOVERY_FAILURES", metric
    )
    monkeypatch.setattr(anonymous_recovery_adapter, "logger", logger)
    assert (
        anonymous_recovery_adapter.request_password_recovery(command=_command()) is None
    )
    metric.inc.assert_called_once_with()
    logger.warning.assert_called_once_with("anonymous_password_recovery_failed")


@pytest.mark.parametrize(
    "surface",
    [
        web_auth.forgot_password_submit,
        web_customer_auth.customer_forgot_password_submit,
        web_reseller_auth.reseller_forgot_password_submit,
    ],
)
def test_web_public_shape_unchanged_and_caller_work_untouched(
    surface, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    @contextmanager
    def failed_lifecycle() -> Generator[Session, None, None]:
        raise RuntimeError(_PRIVATE)
        yield  # pragma: no cover

    monkeypatch.setattr(
        anonymous_recovery_adapter.db_session_adapter,
        "owner_command_session",
        failed_lifecycle,
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/forgot-password",
            "headers": [],
            "query_string": b"",
        }
    )
    transaction = db_session.begin()
    response = surface(request=request, db=db_session, email=_PRIVATE)
    assert response.status_code == 200
    assert "Check your email" in response.body.decode()
    assert db_session.get_transaction() is transaction
    assert transaction.is_active


def test_failure_metric_has_no_labels() -> None:
    assert anonymous_recovery_adapter.ANONYMOUS_RECOVERY_FAILURES._labelnames == ()


@pytest.mark.parametrize("flushed", [False, True])
def test_independent_file_sqlite_session_preserves_pending_caller_state(
    tmp_path, monkeypatch: pytest.MonkeyPatch, flushed: bool
) -> None:
    # Non-authoritative unit evidence for session isolation, not schema parity.
    from sqlalchemy.orm import registry

    engine = create_engine(f"sqlite:///{tmp_path / 'recovery-session-test.db'}")
    mapping = registry(metadata=MetaData())
    marker_table = Table(
        "caller_marker", mapping.metadata, Column("id", Integer, primary_key=True)
    )

    class Marker:
        id: int

    mapping.map_imperatively(Marker, marker_table)
    mapping.metadata.create_all(engine)
    caller = Session(bind=engine)
    marker = Marker()
    marker.id = 1
    caller.add(marker)
    if flushed:
        caller.flush()
    transaction = caller.get_transaction()
    owner_sessions: list[Session] = []

    def create_session() -> Session:
        session = Session(bind=engine)
        owner_sessions.append(session)
        return session

    def failed_owner(
        *, db: Session, command: credential_recovery.RequestPasswordRecoveryCommand
    ) -> None:
        assert db is not caller
        assert db.get_bind() is engine
        assert db.execute(select(marker_table.c.id)).all() == []
        raise RuntimeError(_PRIVATE)

    monkeypatch.setattr(session_module, "SessionLocal", create_session)
    monkeypatch.setattr(credential_recovery, "request_password_recovery", failed_owner)
    try:
        anonymous_recovery_adapter.request_password_recovery(command=_command())
        assert caller.get_transaction() is transaction
        assert transaction is not None and transaction.is_active
        assert marker in caller
        assert owner_sessions and not owner_sessions[0].in_transaction()
        caller.commit()
        with Session(bind=engine) as reader:
            assert reader.execute(select(marker_table.c.id)).scalars().all() == [1]
    finally:
        caller.close()
        mapping.dispose()
        engine.dispose()
