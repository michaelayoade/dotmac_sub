"""Hosted migrated PostgreSQL completion proof sources; collect only locally."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from queue import Queue

import pytest
from jose import jwt
from sqlalchemy import select, text

from app.models.audit import AuditEvent
from app.models.subscriber import Subscriber
from app.services import auth_flow
from app.services import email_verification as owner
from app.services.owner_commands import CommandContext
from tests.integration import test_email_verification_intents_pg as request_pg
from tests.integration.test_credential_administration_concurrency import (
    wait_for_exact_lock,
)

factory = request_pg.factory
target = request_pg.target

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def signing_secret(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "completion-pg-test-secret")


def _command(token):
    return owner.CompleteEmailVerificationCommand(
        context=CommandContext.system(
            actor="capability:email-verification",
            scope=owner.EMAIL_VERIFICATION_SCOPE,
            reason="Hosted completion lock proof",
        ),
        token=token,
    )


def _token(factory, target):
    with factory() as db:
        subscriber = db.get(Subscriber, target)
        return auth_flow._issue_email_verification_token(
            db, str(target), subscriber.email
        )


@pytest.mark.parametrize("intervening_change", ["email", "expiry"])
def test_completion_waits_on_exact_subscriber_then_refuses_stale_proof(
    factory, target, engine, monkeypatch, intervening_change
):
    token = _token(factory, target)
    command = _command(token)
    clock = [datetime.now(UTC)]
    monkeypatch.setattr(owner, "_now", lambda: clock[0])
    pids: Queue[int] = Queue()
    original = owner._subscriber

    def capture(db, subscriber_id, *, lock):
        if lock:
            pids.put(db.scalar(text("SELECT pg_backend_pid()")))
        return original(db, subscriber_id, lock=lock)

    monkeypatch.setattr(owner, "_subscriber", capture)

    def complete():
        with factory() as db:
            with pytest.raises(owner.EmailVerificationError) as caught:
                owner.complete_email_verification(db, command=command)
            return caught.value.code

    with factory() as blocker:
        blocker_pid = blocker.scalar(text("SELECT pg_backend_pid()"))
        subscriber = blocker.scalar(
            select(Subscriber).where(Subscriber.id == target).with_for_update()
        )
        if intervening_change == "email":
            subscriber.email = "changed-during-completion@example.test"
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(complete)
            try:
                wait_for_exact_lock(
                    engine, waiter_pid=pids.get(timeout=3), blocker_pid=blocker_pid
                )
                if intervening_change == "expiry":
                    clock[0] = datetime.fromtimestamp(
                        jwt.get_unverified_claims(token)["exp"], UTC
                    )
            finally:
                blocker.commit()
            assert (
                result.result(timeout=15)
                == "auth.email_verification.invalid_capability"
            )
    with factory() as check:
        subscriber = check.get(Subscriber, target)
        assert not subscriber.email_verified
        if intervening_change == "email":
            assert subscriber.email == "changed-during-completion@example.test"
        assert not check.scalars(
            select(AuditEvent).where(
                AuditEvent.entity_id == str(target),
                AuditEvent.action == "auth.email_verified",
            )
        ).all()


def test_completion_adapter_lock_timeout_preserves_real_flushed_caller(
    factory, target, monkeypatch
):
    from fastapi import HTTPException

    from app.api.auth_flow import verify_email_endpoint
    from app.schemas.auth_flow import VerifyEmailRequest
    from app.services.db_session_adapter import db_session_adapter

    monkeypatch.setattr(db_session_adapter, "owner_command_session", factory)
    observed_codes = []
    original = owner.complete_email_verification

    def capture_failure(db, *, command):
        try:
            return original(db, command=command)
        except owner.EmailVerificationError as exc:
            observed_codes.append(exc.code)
            raise

    monkeypatch.setattr(owner, "complete_email_verification", capture_failure)
    token = _token(factory, target)
    with factory() as caller:
        subscriber = caller.get(Subscriber, target)
        subscriber.first_name = "Retained completion caller write"
        caller.flush()
        with pytest.raises(HTTPException) as caught:
            verify_email_endpoint(payload=VerifyEmailRequest(token=token), db=caller)
        assert caught.value.status_code == 503
        assert observed_codes == ["auth.email_verification.lock_timeout"]
        assert caught.value.headers == {"Retry-After": "5"}
        assert caller.in_transaction()
        caller.commit()
    with factory() as check:
        subscriber = check.get(Subscriber, target)
        assert subscriber.first_name == "Retained completion caller write"
        assert not subscriber.email_verified
        assert not check.scalars(
            select(AuditEvent).where(
                AuditEvent.entity_id == str(target),
                AuditEvent.action == "auth.email_verified",
            )
        ).all()
