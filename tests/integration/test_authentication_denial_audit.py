"""Migrated PostgreSQL failure-side owner evidence; collect until target assigned."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models.audit import AuditEvent
from app.models.auth import UserCredential
from app.services import password_authentication as pa
from tests.integration import test_password_auth_credential_race as credential_race

account = credential_race.account
factory = credential_race.factory

pytestmark = pytest.mark.integration


@pytest.fixture
def independent_failure_sessions(factory, monkeypatch):
    @contextmanager
    def owner_session() -> Iterator[Session]:
        with factory() as db:
            yield db

    monkeypatch.setattr(pa.db_session_adapter, "owner_command_session", owner_session)


def _decision(credential_id):
    return pa.denial_for_request(
        reason=pa.DenialReason.INVALID_CREDENTIALS,
        surface=pa.DenialSurface.LOGIN,
        phase=pa.DenialPhase.PASSWORD,
        target=pa.DenialTarget(pa.DenialTargetKind.CREDENTIAL, credential_id),
        effect=pa.PasswordFailureEffect(credential_id, pa.FailurePolicy(100, 15)),
    )


@pytest.mark.parametrize("flushed", [False, True])
def test_failure_backend_is_independent_and_preserves_request_writes(
    factory,
    account,
    independent_failure_sessions,
    monkeypatch,
    flushed,
):
    actual_stage = pa.stage_audit_event
    failure_pids: list[int] = []

    def capture_backend(db, **kwargs):
        failure_pids.append(db.scalar(text("SELECT pg_backend_pid()")))
        return actual_stage(db, **kwargs)

    monkeypatch.setattr(pa, "stage_audit_event", capture_backend)
    unrelated_id = uuid4()
    decision = _decision(account.credential_id)
    with factory() as request:
        request_pid = request.scalar(text("SELECT pg_backend_pid()"))
        unrelated = AuditEvent(
            id=unrelated_id,
            action="test.unrelated_request",
            entity_type="test",
        )
        request.add(unrelated)
        if flushed:
            request.flush()
        pa.record_authentication_denial(decision)
        assert len(failure_pids) == 1
        assert failure_pids[0] != request_pid
        assert request.in_transaction()
        if flushed:
            assert request.get(AuditEvent, unrelated_id) is unrelated
        else:
            assert unrelated in request.new
        with factory() as observer:
            assert observer.get(AuditEvent, unrelated_id) is None
            credential = observer.get(UserCredential, account.credential_id)
            assert credential.failed_login_attempts == 1
            evidence = observer.scalars(
                select(AuditEvent).where(
                    AuditEvent.action == "auth.authentication_denied",
                    AuditEvent.entity_id == str(account.credential_id),
                )
            ).all()
            assert len(evidence) == 1
            assert evidence[0].metadata_["attempt_id"] == str(decision.attempt_id)
        request.rollback()
    with factory() as observer:
        assert observer.get(AuditEvent, unrelated_id) is None


def test_audit_outage_rolls_back_only_new_failure_counter(
    factory,
    account,
    independent_failure_sessions,
    monkeypatch,
):
    def unavailable(*args, **kwargs):
        raise RuntimeError("synthetic audit outage")

    monkeypatch.setattr(pa, "stage_audit_event", unavailable)
    pa.record_authentication_denial(_decision(account.credential_id))
    with factory() as observer:
        credential = observer.get(UserCredential, account.credential_id)
        assert credential.failed_login_attempts == 0
        assert credential.last_login_at is None
        assert (
            observer.scalars(
                select(AuditEvent).where(
                    AuditEvent.action == "auth.authentication_denied",
                    AuditEvent.entity_id == str(account.credential_id),
                )
            ).all()
            == []
        )


def test_parallel_denials_commit_exactly_one_counter_and_audit_per_attempt(
    factory,
    account,
    independent_failure_sessions,
):
    decisions = [_decision(account.credential_id) for _ in range(8)]
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(pa.record_authentication_denial, decisions))
    with factory() as observer:
        credential = observer.get(UserCredential, account.credential_id)
        assert credential.failed_login_attempts == len(decisions)
        assert credential.credential_version == 1
        assert credential.last_login_at is None
        rows = observer.scalars(
            select(AuditEvent).where(
                AuditEvent.action == "auth.authentication_denied",
                AuditEvent.entity_id == str(account.credential_id),
            )
        ).all()
        assert {row.metadata_["attempt_id"] for row in rows} == {
            str(decision.attempt_id) for decision in decisions
        }
