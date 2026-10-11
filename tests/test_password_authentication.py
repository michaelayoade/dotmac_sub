"""Credential-standing owner: unit lane (SQLite, no concurrency).

The ordering/blocking proofs live in
``tests/integration/test_password_auth_credential_race.py`` (PostgreSQL). Here
the "race" is simulated deterministically: a spy on the password verifier
changes the credential AFTER verification and BEFORE the commit gate, which is
exactly the window the defect lived in.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pyotp
import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from sqlalchemy import select, update

from app.models.audit import AuditEvent
from app.models.auth import (
    AuthProvider,
    MFAMethod,
    MFARecoveryCode,
    SessionStatus,
    UserCredential,
)
from app.models.auth import Session as AuthSession
from app.models.catalog import AccessCredential
from app.services import auth_flow as auth_flow_service
from app.services import password_authentication as pa
from app.services.auth_flow import AuthFlow, change_password, hash_password
from tests.test_auth_flow import _make_request, _system_user_with_credential


@pytest.fixture(autouse=True)
def _signing(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("TOTP_ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))


def _credential(db_session, person, *, username="user@example.com", **extra):
    credential = UserCredential(
        person_id=person.id,
        provider=AuthProvider.local,
        username=username,
        password_hash=hash_password("secret"),
        is_active=True,
        **extra,
    )
    db_session.add(credential)
    db_session.commit()
    return credential


def _enable_mfa(db_session, person):
    setup = AuthFlow.mfa_setup(db_session, str(person.id), label="device")
    AuthFlow.mfa_confirm(
        db_session,
        str(setup["method_id"]),
        pyotp.TOTP(setup["secret"]).now(),
        str(person.id),
    )
    return setup["secret"], setup["method_id"]


def _row(db_session, credential_id):
    db_session.expire_all()
    return db_session.execute(
        select(
            UserCredential.credential_version,
            UserCredential.failed_login_attempts,
            UserCredential.locked_until,
            UserCredential.last_login_at,
            UserCredential.password_hash,
            UserCredential.must_change_password,
            UserCredential.updated_at,
        ).where(UserCredential.id == credential_id)
    ).one()


def _sessions(db_session):
    db_session.expire_all()
    return db_session.query(AuthSession).count()


def _audit_actions(db_session) -> list[str]:
    db_session.expire_all()
    return [
        row.action
        for row in db_session.query(AuditEvent)
        .filter(AuditEvent.action.like("auth.%"))
        .order_by(AuditEvent.occurred_at)
        .all()
    ]


def _after_verify(monkeypatch, db_session, mutate):
    """Run ``mutate`` once, right after the first successful verification."""

    real = auth_flow_service.verify_password
    done = {"n": 0}

    def spy(password, password_hash):
        ok = real(password, password_hash)
        if ok and not done["n"]:
            done["n"] = 1
            mutate()
            db_session.commit()
        return ok

    monkeypatch.setattr(auth_flow_service, "verify_password", spy)


# --------------------------------------------------------------------------
# Challenge format (section 6)
# --------------------------------------------------------------------------


def _verified(credential, person, **overrides):
    values = {
        "src": pa.SRC_USER_CREDENTIAL,
        "principal_type": "subscriber",
        "principal_id": str(person.id),
        "audience": "general",
        "staff_binding": None,
        "mfa_required": True,
        "enrollment_required": False,
        "credential_id": credential.id,
        "credential_version": 1,
        "provider": AuthProvider.local,
    }
    values.update(overrides)
    return pa.VerifiedCredential(**values)


def _encode(payload):
    return auth_flow_service._jwt_encode_token(  # noqa: SLF001
        payload, "test-secret", "HS256"
    )


def _payload(credential, person, **overrides):
    payload = pa._challenge_payload(  # noqa: SLF001
        _verified(credential, person),
        typ="mfa",
        credential_version=1,
        now=datetime.now(UTC),
    )
    payload.update(overrides)
    return {k: v for k, v in payload.items() if v is not None}


def test_challenge_round_trips_with_binding(db_session, person):
    credential = _credential(db_session, person)
    challenge = pa.decode_challenge(
        db_session, _encode(_payload(credential, person)), expected_typ="mfa"
    )
    assert challenge.credential_id == credential.id
    assert challenge.credential_version == 1
    assert challenge.audience == "general"
    assert challenge.src == pa.SRC_USER_CREDENTIAL


@pytest.mark.parametrize("missing", ["cid", "cv", "ver", "src", "login_aud"])
def test_challenge_missing_binding_claim_is_refused(db_session, person, missing):
    credential = _credential(db_session, person)
    payload = _payload(credential, person)
    payload.pop(missing)
    with pytest.raises(pa.PasswordAuthenticationError) as excinfo:
        pa.decode_challenge(db_session, _encode(payload), expected_typ="mfa")
    assert excinfo.value.kind == "invalid_mfa_token"


def test_challenge_version_one_is_refused(db_session, person):
    credential = _credential(db_session, person)
    payload = _payload(credential, person, ver=1)
    with pytest.raises(pa.PasswordAuthenticationError):
        pa.decode_challenge(db_session, _encode(payload), expected_typ="mfa")


def test_challenge_unknown_source_and_radius_user_are_refused(db_session, person):
    credential = _credential(db_session, person)
    for src in ("radius_user", "mystery"):
        with pytest.raises(pa.PasswordAuthenticationError):
            pa.decode_challenge(
                db_session,
                _encode(_payload(credential, person, src=src)),
                expected_typ="mfa",
            )


def test_challenge_carries_no_secret_material(db_session, person):
    credential = _credential(db_session, person)
    token = _encode(_payload(credential, person))
    payload = auth_flow_service._jwt_decode_token(  # noqa: SLF001
        token, "test-secret", "HS256"
    )
    assert set(payload) == {
        "typ",
        "ver",
        "sub",
        "principal_id",
        "principal_type",
        "login_aud",
        "src",
        "iat",
        "exp",
        "jti",
        "cid",
        "cv",
    }
    assert credential.password_hash not in str(payload)


def test_mfa_verify_refuses_audience_mismatch(db_session, person):
    _credential(db_session, person)
    secret, _method = _enable_mfa(db_session, person)
    result = AuthFlow.login(
        db_session, "user@example.com", "secret", _make_request(), None
    )
    with pytest.raises(HTTPException) as excinfo:
        AuthFlow.mfa_verify(
            db_session,
            result["mfa_token"],
            pyotp.TOTP(secret).now(),
            _make_request(),
            audience=auth_flow_service.LoginAudience.admin,
        )
    assert excinfo.value.status_code == 401
    assert excinfo.value.detail == "Invalid MFA token"
    assert _sessions(db_session) == 0


def test_mfa_verify_refuses_legacy_token_without_fallback(db_session, person):
    credential = _credential(db_session, person)
    _enable_mfa(db_session, person)
    legacy = auth_flow_service._issue_mfa_token(  # noqa: SLF001
        db_session, str(person.id), "subscriber"
    )
    with pytest.raises(HTTPException) as excinfo:
        AuthFlow.mfa_verify(db_session, legacy, "123456", _make_request())
    assert excinfo.value.status_code == 401
    assert _sessions(db_session) == 0
    assert _row(db_session, credential.id).last_login_at is None


# --------------------------------------------------------------------------
# MFA-pending password step (D3): no session, no last_login_at, no success audit
# --------------------------------------------------------------------------


def test_mfa_pending_password_step_commits_standing_only(db_session, person):
    credential = _credential(db_session, person, failed_login_attempts=3)
    _enable_mfa(db_session, person)

    result = AuthFlow.login(
        db_session, "user@example.com", "secret", _make_request(), None
    )

    assert result["mfa_required"] is True
    row = _row(db_session, credential.id)
    assert row.failed_login_attempts == 0
    assert row.last_login_at is None
    assert _sessions(db_session) == 0
    actions = _audit_actions(db_session)
    assert "auth.password_step_succeeded" in actions
    assert "auth.login_succeeded" not in actions


def test_full_login_stamps_last_login_and_stages_success_audit(db_session, person):
    credential = _credential(db_session, person)

    result = AuthFlow.login(
        db_session, "user@example.com", "secret", _make_request(), None
    )

    assert result["access_token"] and result["refresh_token"]
    assert _row(db_session, credential.id).last_login_at is not None
    assert _sessions(db_session) == 1
    assert _audit_actions(db_session) == ["auth.login_succeeded"]
    event = db_session.query(AuditEvent).filter_by(action="auth.login_succeeded").one()
    metadata = event.metadata_
    assert metadata["mfa"] is False
    assert metadata["credential_version"] == 1
    assert metadata["src"] == "user_credential"
    blob = str(metadata) + str(event.details)
    assert "secret" not in blob and credential.password_hash not in blob


def test_mfa_completion_stamps_last_login_and_audits(db_session, person):
    credential = _credential(db_session, person)
    secret, _method = _enable_mfa(db_session, person)
    step = AuthFlow.login(
        db_session, "user@example.com", "secret", _make_request(), None
    )

    tokens = AuthFlow.mfa_verify(
        db_session, step["mfa_token"], pyotp.TOTP(secret).now(), _make_request()
    )

    assert tokens["access_token"]
    assert _row(db_session, credential.id).last_login_at is not None
    assert _sessions(db_session) == 1
    assert _audit_actions(db_session) == [
        "auth.password_step_succeeded",
        "auth.login_succeeded",
    ]


# --------------------------------------------------------------------------
# R8: PPPoE authentication never touches the local credential
# --------------------------------------------------------------------------


def test_pppoe_login_leaves_user_credential_byte_identical(db_session, person):
    credential = _credential(
        db_session,
        person,
        failed_login_attempts=2,
        username="portal-user@example.com",
    )
    db_session.add(
        AccessCredential(
            subscriber_id=person.id,
            username="pppoe-r8-001",
            secret_hash=auth_flow_service.hash_service_secret("pppoe-secret"),
            is_active=True,
        )
    )
    db_session.commit()
    before = _row(db_session, credential.id)

    result = AuthFlow.login(
        db_session, "pppoe-r8-001", "pppoe-secret", _make_request(), None
    )

    assert result["access_token"]
    assert _row(db_session, credential.id) == before
    assert _row(db_session, credential.id).failed_login_attempts == 2
    assert _row(db_session, credential.id).last_login_at is None
    event = db_session.query(AuditEvent).filter_by(action="auth.login_succeeded").one()
    assert event.metadata_["src"] == "access_credential"
    assert event.metadata_["credential_id"] is None


def test_pppoe_mfa_challenge_binds_access_credential_and_voids_on_change(
    db_session, person
):
    access = AccessCredential(
        subscriber_id=person.id,
        username="pppoe-r8-mfa",
        secret_hash=auth_flow_service.hash_service_secret("pppoe-secret"),
        is_active=True,
    )
    db_session.add(access)
    db_session.commit()
    secret, _method = _enable_mfa(db_session, person)
    step = AuthFlow.login(
        db_session, "pppoe-r8-mfa", "pppoe-secret", _make_request(), None
    )
    challenge = pa.decode_challenge(db_session, step["mfa_token"], expected_typ="mfa")
    assert challenge.src == pa.SRC_ACCESS_CREDENTIAL
    assert challenge.access_credential_id == access.id

    db_session.execute(
        update(AccessCredential)
        .where(AccessCredential.id == access.id)
        .values(secret_hash=auth_flow_service.hash_service_secret("rotated"))
    )
    db_session.commit()

    with pytest.raises(HTTPException) as excinfo:
        AuthFlow.mfa_verify(
            db_session, step["mfa_token"], pyotp.TOTP(secret).now(), _make_request()
        )
    assert excinfo.value.status_code == 401
    assert _sessions(db_session) == 0


# --------------------------------------------------------------------------
# Phase A / phase B boundaries
# --------------------------------------------------------------------------


def test_phase_a_with_dirty_session_raises_instead_of_discarding(db_session, person):
    credential = _credential(db_session, person)
    credential.failed_login_attempts = 99  # pending, unflushed write

    with pytest.raises(RuntimeError, match="read-only"):
        pa.release_read_transaction(db_session)

    assert credential.failed_login_attempts == 99


def test_login_with_pending_writes_is_a_programming_error(db_session, person):
    credential = _credential(db_session, person)
    credential.failed_login_attempts = 1

    with pytest.raises(RuntimeError):
        AuthFlow.login(db_session, "user@example.com", "secret", _make_request(), None)


@pytest.mark.parametrize(
    ("mutation", "status", "expected_attempts"),
    [
        ({"must_change_password": True}, 428, 0),
        ({"locked_until": "future"}, 403, 0),
        ({"credential_version": 2}, 401, 0),
        ({"is_active": False}, 401, 0),
    ],
)
def test_gate_miss_is_classified_and_never_counts_a_failure(
    db_session, person, monkeypatch, mutation, status, expected_attempts
):
    credential = _credential(db_session, person)
    values = {
        key: (datetime.now(UTC) + timedelta(minutes=10) if value == "future" else value)
        for key, value in mutation.items()
    }
    _after_verify(
        monkeypatch,
        db_session,
        lambda: db_session.execute(
            update(UserCredential)
            .where(UserCredential.id == credential.id)
            .values(**values)
        ),
    )

    with pytest.raises(HTTPException) as excinfo:
        AuthFlow.login(db_session, "user@example.com", "secret", _make_request(), None)

    assert excinfo.value.status_code == status
    row = _row(db_session, credential.id)
    assert row.failed_login_attempts == expected_attempts
    assert row.last_login_at is None
    assert _sessions(db_session) == 0
    assert "auth.login_succeeded" not in _audit_actions(db_session)


def test_reset_between_verify_and_gate_mints_no_session(
    db_session, person, monkeypatch
):
    """R1: the stale verification must not outlive a reset."""

    credential = _credential(db_session, person)
    _after_verify(
        monkeypatch,
        db_session,
        lambda: db_session.execute(
            update(UserCredential)
            .where(UserCredential.id == credential.id)
            .values(
                password_hash=hash_password("Reset-by-owner1!"),
                credential_version=UserCredential.credential_version + 1,
            )
        ),
    )

    with pytest.raises(HTTPException) as excinfo:
        AuthFlow.login(db_session, "user@example.com", "secret", _make_request(), None)

    assert excinfo.value.status_code == 401
    assert _sessions(db_session) == 0
    row = _row(db_session, credential.id)
    assert row.credential_version == 2
    assert row.last_login_at is None


def test_audit_failure_inside_phase_b_fails_closed(db_session, person, monkeypatch):
    credential = _credential(db_session, person, failed_login_attempts=2)

    def boom(*_args, **_kwargs):
        raise RuntimeError("audit store unavailable")

    monkeypatch.setattr(pa, "stage_audit_event", boom)

    with pytest.raises(RuntimeError):
        AuthFlow.login(db_session, "user@example.com", "secret", _make_request(), None)

    row = _row(db_session, credential.id)
    assert _sessions(db_session) == 0
    assert row.last_login_at is None
    assert row.failed_login_attempts == 2  # the gate rolled back with the audit


def test_staff_login_session_has_party_and_presence_rolls_back_with_gate(
    db_session, monkeypatch
):
    system_user, credential = _system_user_with_credential(
        db_session, "staff-race@example.com"
    )
    result = AuthFlow.login(
        db_session, system_user.email, "secret", _make_request(), None
    )
    assert result["access_token"]
    session = db_session.query(AuthSession).one()
    assert session.party_id is not None
    assert session.system_user_id == system_user.id

    # A presence failure rolls the gate back too.
    from app.services import team_inbox_assignment

    def boom(*_args, **_kwargs):
        raise RuntimeError("presence unavailable")

    monkeypatch.setattr(team_inbox_assignment, "record_agent_signed_in_presence", boom)
    before = _row(db_session, credential.id)
    with pytest.raises(RuntimeError):
        AuthFlow.login(db_session, system_user.email, "secret", _make_request(), None)
    after = _row(db_session, credential.id)
    assert after.last_login_at == before.last_login_at
    assert _sessions(db_session) == 1


# --------------------------------------------------------------------------
# MFA completion gate and recovery codes (R2, R6)
# --------------------------------------------------------------------------


def test_reset_between_password_step_and_mfa_completion_voids_challenge(
    db_session, person
):
    credential = _credential(db_session, person)
    secret, method_id = _enable_mfa(db_session, person)
    method = db_session.get(MFAMethod, method_id)
    codes = auth_flow_service.generate_mfa_recovery_codes(db_session, method)
    step = AuthFlow.login(
        db_session, "user@example.com", "secret", _make_request(), None
    )
    last_used_before = db_session.get(MFAMethod, method_id).last_used_at

    db_session.execute(
        update(UserCredential)
        .where(UserCredential.id == credential.id)
        .values(
            password_hash=hash_password("Reset-by-owner1!"),
            credential_version=UserCredential.credential_version + 1,
        )
    )
    db_session.commit()

    with pytest.raises(HTTPException) as excinfo:
        AuthFlow.mfa_verify(db_session, step["mfa_token"], codes[0], _make_request())

    assert excinfo.value.status_code == 401
    assert excinfo.value.detail == "Invalid MFA token"
    assert _sessions(db_session) == 0
    unused = (
        db_session.query(MFARecoveryCode)
        .filter(MFARecoveryCode.is_active.is_(True))
        .count()
    )
    assert unused == len(codes)
    db_session.expire_all()
    assert db_session.get(MFAMethod, method_id).last_used_at == last_used_before


def test_deactivated_credential_voids_challenge(db_session, person):
    credential = _credential(db_session, person)
    secret, _method = _enable_mfa(db_session, person)
    step = AuthFlow.login(
        db_session, "user@example.com", "secret", _make_request(), None
    )
    db_session.execute(
        update(UserCredential)
        .where(UserCredential.id == credential.id)
        .values(is_active=False)
    )
    db_session.commit()
    with pytest.raises(HTTPException) as excinfo:
        AuthFlow.mfa_verify(
            db_session, step["mfa_token"], pyotp.TOTP(secret).now(), _make_request()
        )
    assert excinfo.value.status_code == 401


def test_representation_rehash_does_not_void_challenge(db_session, person):
    """D3: same secret, new representation -> pending challenge still works."""

    credential = _credential(db_session, person)
    secret, _method = _enable_mfa(db_session, person)
    step = AuthFlow.login(
        db_session, "user@example.com", "secret", _make_request(), None
    )
    row = _row(db_session, credential.id)

    assert pa.replace_password_representation(
        db_session,
        credential_id=credential.id,
        expected_version=row.credential_version,
        expected_hash=row.password_hash,
        new_hash=hash_password("secret"),
    )
    db_session.commit()
    assert _row(db_session, credential.id).credential_version == 1

    tokens = AuthFlow.mfa_verify(
        db_session, step["mfa_token"], pyotp.TOTP(secret).now(), _make_request()
    )
    assert tokens["access_token"]


def test_representation_hook_refuses_on_drift(db_session, person):
    credential = _credential(db_session, person)
    assert not pa.replace_password_representation(
        db_session,
        credential_id=credential.id,
        expected_version=7,
        expected_hash=credential.password_hash,
        new_hash="x",
    )
    assert not pa.replace_password_representation(
        db_session,
        credential_id=credential.id,
        expected_version=1,
        expected_hash="not-the-hash",
        new_hash="x",
    )
    db_session.commit()


def test_recovery_code_is_spent_once_and_failure_is_counted(db_session, person):
    _credential(db_session, person)
    secret, method_id = _enable_mfa(db_session, person)
    method = db_session.get(MFAMethod, method_id)
    code = auth_flow_service.generate_mfa_recovery_codes(db_session, method)[0]
    first = AuthFlow.login(
        db_session, "user@example.com", "secret", _make_request(), None
    )
    second = AuthFlow.login(
        db_session, "user@example.com", "secret", _make_request(), None
    )

    assert AuthFlow.mfa_verify(db_session, first["mfa_token"], code, _make_request())
    with pytest.raises(HTTPException) as excinfo:
        AuthFlow.mfa_verify(db_session, second["mfa_token"], code, _make_request())

    assert excinfo.value.status_code == 401
    assert excinfo.value.detail == "Invalid MFA code"
    assert _sessions(db_session) == 1
    db_session.expire_all()
    assert db_session.get(MFAMethod, method_id).failed_attempts == 1


# --------------------------------------------------------------------------
# Failure counters (R5, R9)
# --------------------------------------------------------------------------


def test_failure_statement_increments_locks_and_never_extends(db_session, person):
    credential = _credential(db_session, person)
    policy = pa.FailurePolicy(max_attempts=3, lock_minutes=10)
    now = datetime.now(UTC)

    def hit(at):
        row = db_session.execute(
            pa.password_failure_statement(credential.id, now=at, policy=policy)
        ).one()
        db_session.commit()
        return row.failed_login_attempts, auth_flow_service._as_utc(  # noqa: SLF001
            row.locked_until
        )

    assert hit(now) == (1, None)
    assert hit(now) == (2, None)
    attempts, locked = hit(now)
    assert attempts == 3 and locked is not None
    locked_at = locked
    # Already locked: unchanged, never extended.
    assert hit(now + timedelta(minutes=1)) == (3, locked_at)
    # Expired lock: a fresh window of one, not an immediate re-lock.
    attempts, locked = hit(locked_at + timedelta(seconds=1))
    assert (attempts, locked) == (1, None)


def test_expired_lock_is_not_reset_by_a_dirty_orm_write_before_verification(
    db_session, person
):
    """R9: nothing is written before the password is verified."""

    credential = _credential(
        db_session,
        person,
        failed_login_attempts=5,
        locked_until=datetime.now(UTC) - timedelta(minutes=1),
    )
    with pytest.raises(HTTPException) as excinfo:
        AuthFlow.login(db_session, "user@example.com", "WRONG", _make_request(), None)
    assert excinfo.value.status_code == 401
    row = _row(db_session, credential.id)
    # Wrong password after an expired lock: fresh window of one.
    assert row.failed_login_attempts == 1
    assert row.locked_until is None


def test_wrong_password_locks_at_the_configured_maximum(db_session, person):
    credential = _credential(db_session, person)
    for _ in range(auth_flow_service.LOGIN_MAX_FAILED_ATTEMPTS):
        with pytest.raises(HTTPException):
            AuthFlow.login(db_session, "user@example.com", "bad", _make_request(), None)
    assert _row(db_session, credential.id).locked_until is not None
    with pytest.raises(HTTPException) as excinfo:
        AuthFlow.login(db_session, "user@example.com", "secret", _make_request(), None)
    assert excinfo.value.status_code == 403


def test_failure_counter_write_error_still_denies(db_session, person, monkeypatch):
    _credential(db_session, person)

    def boom(*_args, **_kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(pa, "password_failure_statement", boom)
    with pytest.raises(HTTPException) as excinfo:
        AuthFlow.login(db_session, "user@example.com", "bad", _make_request(), None)
    assert excinfo.value.status_code == 401


# --------------------------------------------------------------------------
# Authenticated password change (R7) and reset bump
# --------------------------------------------------------------------------


def test_change_password_bumps_version_revokes_others_and_audits(db_session, person):
    credential = _credential(db_session, person)
    keep = AuthFlow.login(
        db_session, "user@example.com", "secret", _make_request(), None
    )
    AuthFlow.login(db_session, "user@example.com", "secret", _make_request(), None)
    sessions = db_session.query(AuthSession).all()
    keep_id = sessions[0].id

    changed_at = change_password(
        db_session,
        str(person.id),
        "secret",
        "Brandnew1!",
        current_session_id=str(keep_id),
    )

    assert keep["access_token"] and changed_at
    row = _row(db_session, credential.id)
    assert row.credential_version == 2
    assert auth_flow_service.verify_password("Brandnew1!", row.password_hash)
    db_session.expire_all()
    statuses = {s.id: s.status for s in db_session.query(AuthSession).all()}
    assert statuses[keep_id] is SessionStatus.active
    assert sum(1 for s in statuses.values() if s is SessionStatus.revoked) == 1
    event = db_session.query(AuditEvent).filter_by(action="auth.password_changed").one()
    assert event.metadata_["credential_version"] == 2
    assert event.metadata_["sessions_revoked"] == 1
    assert "Brandnew1!" not in str(event.metadata_)


def test_change_password_refuses_when_a_reset_committed_after_verify(
    db_session, person, monkeypatch
):
    credential = _credential(db_session, person)
    reset_hash = hash_password("Reset-by-owner1!")
    _after_verify(
        monkeypatch,
        db_session,
        lambda: db_session.execute(
            update(UserCredential)
            .where(UserCredential.id == credential.id)
            .values(
                password_hash=reset_hash,
                credential_version=UserCredential.credential_version + 1,
            )
        ),
    )

    with pytest.raises(HTTPException) as excinfo:
        change_password(db_session, str(person.id), "secret", "Brandnew1!")

    assert excinfo.value.status_code == 409
    row = _row(db_session, credential.id)
    assert row.password_hash == reset_hash  # the reset was not overwritten
    assert row.credential_version == 2


def test_password_reset_bumps_credential_version(db_session, person):
    from app.services.auth_flow import request_password_reset, reset_password

    credential = _credential(db_session, person)
    person.email = "reset-bump@example.com"
    credential.username = person.email
    db_session.commit()
    result = request_password_reset(db_session, person.email)
    reset_password(db_session, result["token"], "Brandnew1!")
    db_session.commit()
    assert _row(db_session, credential.id).credential_version == 2


def test_credential_version_is_not_exposed_by_auth_schemas():
    from app.schemas import auth as auth_schemas

    for name in dir(auth_schemas):
        model = getattr(auth_schemas, name)
        fields = getattr(model, "model_fields", None)
        if isinstance(fields, dict):
            assert "credential_version" not in fields, name
