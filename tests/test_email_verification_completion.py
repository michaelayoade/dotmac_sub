"""Fast-unit owner evidence; hosted migrated PostgreSQL remains authoritative."""

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException
from jose import jwt
from sqlalchemy import event, select, update

from app.models.audit import AuditEvent
from app.models.subscriber import Subscriber
from app.services import auth_flow
from app.services import email_verification as owner
from app.services.owner_commands import CommandContext
from tests.customer_profile_owner_support import profile_owner_session

MISSING_PRINCIPAL = str(uuid4())


def command(token):
    return owner.CompleteEmailVerificationCommand(
        context=CommandContext.system(
            actor="capability:email-verification",
            scope=owner.EMAIL_VERIFICATION_SCOPE,
            reason="Completion owner fast-unit evidence",
        ),
        token=token,
    )


@pytest.fixture(autouse=True)
def secret(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "completion-unit-test-secret")


def token_for(db, subscriber):
    subscriber.email = "exact-proof@example.test"
    subscriber.email_verified = False
    db.commit()
    return auth_flow._issue_email_verification_token(
        db, str(subscriber.id), subscriber.email
    )


def complete(db, token):
    with profile_owner_session(db) as fresh:
        return owner.complete_email_verification(fresh, command=command(token))


def test_completion_is_atomic_safe_and_idempotent(db_session, subscriber, caplog):
    token = token_for(db_session, subscriber)
    assert token not in repr(command(token))
    result = complete(db_session, token)
    assert (result.subscriber_id, result.email_verified, result.changed) == (
        subscriber.id,
        True,
        True,
    )
    again = complete(db_session, token)
    assert again.email_verified and not again.changed
    events = db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == "auth.email_verified")
    ).all()
    assert len(events) == 1
    assert str(events[0].actor_id) == str(subscriber.id)
    evidence = json.dumps(events[0].metadata_) + caplog.text
    assert token not in evidence and subscriber.email not in evidence
    assert events[0].metadata_["proof"] == "email_verification_capability"


@pytest.mark.parametrize(
    "patch",
    [
        {"typ": "access"},
        {"exp": 0},
        {"exp": None},
        {"exp": False},
        {"principal_type": "system_user"},
        {"principal_id": "invalid"},
        {"principal_id": MISSING_PRINCIPAL, "sub": MISSING_PRINCIPAL},
        {"sub": str(uuid4())},
        {"principal_id": str(uuid4()), "sub": str(uuid4())},
        {"email": None},
        {"email": "stale@example.test"},
    ],
)
def test_invalid_proof_is_generic(db_session, subscriber, patch):
    token = token_for(db_session, subscriber)
    claims = jwt.get_unverified_claims(token)
    token = auth_flow._jwt_encode_token(
        {**claims, **patch}, "completion-unit-test-secret", "HS256"
    )
    with pytest.raises(owner.EmailVerificationError) as caught:
        complete(db_session, token)
    assert caught.value.code == "auth.email_verification.invalid_capability"
    assert caught.value.details == {}
    assert token not in str(caught.value)
    db_session.refresh(subscriber)
    assert not subscriber.email_verified


def test_bad_token_translates_legacy_http_error(db_session):
    with pytest.raises(owner.EmailVerificationError) as caught:
        complete(db_session, "not-a-token")
    assert caught.value.code == "auth.email_verification.invalid_capability"


def test_audit_failure_rolls_back_flag(db_session, subscriber, monkeypatch):
    token = token_for(db_session, subscriber)

    def fail(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(owner, "stage_audit_event", fail)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        complete(db_session, token)
    db_session.refresh(subscriber)
    assert not subscriber.email_verified
    assert not db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == "auth.email_verified")
    ).all()


def test_stale_identity_map_is_reloaded_under_lock(db_session, subscriber):
    token = token_for(db_session, subscriber)
    with profile_owner_session(db_session) as fresh:
        fresh.expire_on_commit = False
        stale = fresh.get(Subscriber, subscriber.id)
        fresh.commit()
        db_session.execute(
            update(Subscriber)
            .where(Subscriber.id == subscriber.id)
            .values(email="changed@example.test")
        )
        db_session.commit()
        assert stale.email == "exact-proof@example.test"
        with pytest.raises(owner.EmailVerificationError):
            owner.complete_email_verification(fresh, command=command(token))
    db_session.refresh(subscriber)
    assert not subscriber.email_verified


def test_change_after_decode_before_lock_is_refused(
    db_session, subscriber, monkeypatch
):
    token = token_for(db_session, subscriber)
    original = auth_flow.decode_email_verification_capability

    def decode_and_change(db, *, token):
        payload = original(db, token=token)
        db.execute(
            update(Subscriber)
            .where(Subscriber.id == subscriber.id)
            .values(email="intervening@example.test")
        )
        return payload

    monkeypatch.setattr(
        auth_flow, "decode_email_verification_capability", decode_and_change
    )
    with pytest.raises(owner.EmailVerificationError):
        complete(db_session, token)
    db_session.refresh(subscriber)
    assert not subscriber.email_verified


def test_active_flushed_caller_is_never_rolled_back(db_session, subscriber):
    token = token_for(db_session, subscriber)
    subscriber.first_name = "Retained caller edit"
    db_session.flush()
    with pytest.raises(owner.EmailVerificationError) as caught:
        owner.complete_email_verification(db_session, command=command(token))
    assert caught.value.code == "auth.email_verification.active_caller_transaction"
    assert db_session.in_transaction()
    db_session.expire(subscriber)
    assert subscriber.first_name == "Retained caller edit"


@pytest.mark.parametrize("web", [False, True])
@pytest.mark.parametrize("failure", ["invalid", "storage", None])
def test_adapters_use_fresh_owner_and_preserve_flushed_caller(
    db_session, subscriber, monkeypatch, web, failure
):
    from app.api.auth_flow import verify_email_endpoint
    from app.schemas.auth_flow import VerifyEmailRequest
    from app.services import web_customer_auth
    from app.services.db_session_adapter import db_session_adapter

    token = token_for(db_session, subscriber)
    subscriber.first_name = "Preserved adapter edit"
    db_session.flush()

    @contextmanager
    def fresh_session():
        with profile_owner_session(db_session) as fresh:
            assert fresh is not db_session and not fresh.in_transaction()
            yield fresh

    monkeypatch.setattr(db_session_adapter, "owner_command_session", fresh_session)
    original = owner.complete_email_verification

    def exercise(db, *, command):
        if failure == "invalid":
            raise owner._invalid_capability()
        if failure == "storage":
            raise RuntimeError("sensitive diagnostic must not escape")
        return original(db, command=command)

    monkeypatch.setattr(owner, "complete_email_verification", exercise)
    if web:
        from starlette.requests import Request

        request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
        response = web_customer_auth.customer_verify_email_page(
            request, db_session, token
        )
        assert response.status_code == (
            {"invalid": 400, "storage": 503}.get(failure, 200)
        )
        if failure == "storage":
            assert response.headers["Retry-After"] == "5"
        assert b"sensitive diagnostic" not in response.body
    elif failure:
        with pytest.raises(HTTPException) as caught:
            verify_email_endpoint(
                payload=VerifyEmailRequest(token=token), db=db_session
            )
        assert caught.value.status_code == (401 if failure == "invalid" else 503)
        if failure == "storage":
            assert caught.value.headers == {"Retry-After": "5"}
        assert "sensitive diagnostic" not in str(caught.value.detail)
    else:
        assert verify_email_endpoint(
            payload=VerifyEmailRequest(token=token), db=db_session
        ).email_verified
    assert db_session.in_transaction()
    db_session.expire(subscriber)
    assert subscriber.first_name == "Preserved adapter edit"


def test_decoder_operational_http_failure_is_safe_domain_error(db_session, monkeypatch):
    def unavailable(db, token):
        raise HTTPException(status_code=500, detail="private signing diagnostics")

    monkeypatch.setattr(auth_flow, "_decode_email_verification_token", unavailable)
    with pytest.raises(owner.EmailVerificationError) as caught:
        complete(db_session, "not-persisted")
    assert caught.value.code == "auth.email_verification.unavailable"
    assert "private" not in str(caught.value)
    assert caught.value.retryable


def test_commit_failure_rolls_back_flushed_flag_and_audit(db_session, subscriber):
    token = token_for(db_session, subscriber)
    with profile_owner_session(db_session) as fresh:
        observed = []

        def fail_commit(db):
            observed.append(db.get(Subscriber, subscriber.id).email_verified)
            assert (
                db.scalar(
                    select(AuditEvent).where(AuditEvent.action == "auth.email_verified")
                )
                is not None
            )
            raise RuntimeError("commit storage failure")

        event.listen(fresh, "before_commit", fail_commit)
        with pytest.raises(RuntimeError, match="commit storage failure"):
            owner.complete_email_verification(fresh, command=command(token))
        assert observed == [True]
    db_session.refresh(subscriber)
    assert not subscriber.email_verified
    assert not db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == "auth.email_verified")
    ).all()


def test_verified_flag_does_not_bypass_changed_current_address(db_session, subscriber):
    token = token_for(db_session, subscriber)
    subscriber.email_verified = True
    subscriber.email = "different-verified@example.test"
    db_session.commit()
    with pytest.raises(owner.EmailVerificationError) as caught:
        complete(db_session, token)
    assert caught.value.code == "auth.email_verification.invalid_capability"
    assert not db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == "auth.email_verified")
    ).all()


@pytest.mark.parametrize("already_verified", [False, True])
def test_capability_expiring_during_lock_cannot_mutate_or_succeed(
    db_session, subscriber, monkeypatch, already_verified
):
    token = token_for(db_session, subscriber)
    expires = jwt.get_unverified_claims(token)["exp"]
    subscriber.email_verified = already_verified
    db_session.commit()
    clock = [datetime.now(UTC)]
    monkeypatch.setattr(owner, "_now", lambda: clock[0])
    original = owner._subscriber

    def locked_after_expiry(db, subscriber_id, *, lock):
        assert int(clock[0].timestamp()) < expires
        row = original(db, subscriber_id, lock=lock)
        clock[0] = datetime.fromtimestamp(expires, UTC)
        return row

    monkeypatch.setattr(owner, "_subscriber", locked_after_expiry)
    with pytest.raises(owner.EmailVerificationError) as caught:
        complete(db_session, token)
    assert caught.value.code == "auth.email_verification.invalid_capability"
    db_session.refresh(subscriber)
    assert subscriber.email_verified is already_verified
    assert not db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == "auth.email_verified")
    ).all()
