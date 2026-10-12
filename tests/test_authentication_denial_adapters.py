"""Typed adapter decisions: exact-once evidence and success-owner rollback."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.audit import AuditEvent
from app.models.auth import MFAMethod, MFARecoveryCode, UserCredential
from app.models.auth import Session as AuthSession
from app.services import auth_flow as af
from app.services import password_authentication as pa
from app.services.auth_flow import AuthFlow
from tests.authentication_denial_unit import (
    authentication_denial_unit_session as authentication_denial_unit_session,
)
from tests.test_auth_flow import _make_request
from tests.test_password_authentication import _credential, _enable_mfa

pytestmark = pytest.mark.usefixtures("authentication_denial_unit_session")


@pytest.fixture(autouse=True)
def signing(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("JWT_SECRET", "denial-adapter-unit-secret")
    monkeypatch.setenv("TOTP_ENCRYPTION_KEY", Fernet.generate_key().decode())


def _denials(db):
    return db.scalars(
        select(AuditEvent).where(AuditEvent.action == "auth.authentication_denied")
    ).all()


def test_known_wrong_password_is_one_denial_and_one_counter(db_session, person):
    credential = _credential(db_session, person)
    credential_id = credential.id
    with pytest.raises(HTTPException) as result:
        AuthFlow.login(
            db_session, "user@example.com", "synthetic-wrong", _make_request(), None
        )
    assert result.value.status_code == 401
    db_session.expire_all()
    row = db_session.get(UserCredential, credential_id)
    assert row.failed_login_attempts == 1
    assert row.last_login_at is None
    assert row.credential_version == 1
    evidence = _denials(db_session)
    assert len(evidence) == 1
    assert evidence[0].entity_id == str(credential_id)
    assert db_session.scalars(select(AuthSession)).all() == []


def test_unknown_login_has_no_identity_audit_row(db_session):
    with pytest.raises(HTTPException) as result:
        AuthFlow.login(
            db_session,
            "unresolved@example.test",
            "synthetic-wrong",
            _make_request(),
            None,
        )
    assert result.value.status_code == 401
    assert _denials(db_session) == []


def test_invalid_unsigned_challenge_does_not_attribute_claims(db_session):
    with pytest.raises(HTTPException) as result:
        AuthFlow.mfa_verify(
            db_session, "unsigned.identity.claim", "123456", _make_request()
        )
    assert result.value.status_code == 401
    assert _denials(db_session) == []


def test_unknown_signed_target_is_not_identity_evidence(db_session):
    challenge = pa.Challenge(
        typ="mfa",
        principal_id=str(uuid4()),
        principal_type="subscriber",
        party_id=None,
        audience="general",
        src=pa.SRC_USER_CREDENTIAL,
        credential_id=uuid4(),
        credential_version=1,
        access_credential_id=None,
        access_credential_updated_us=None,
    )
    assert pa.challenge_denial_target(db_session, challenge) is None


def test_wrong_mfa_is_one_counter_and_one_audit(db_session, person):
    _credential(db_session, person)
    _, method_id = _enable_mfa(db_session, person)
    result = AuthFlow.login(
        db_session, "user@example.com", "secret", _make_request(), None
    )
    with pytest.raises(HTTPException) as refusal:
        AuthFlow.mfa_verify(
            db_session, result["mfa_token"], "not-a-code", _make_request()
        )
    assert refusal.value.status_code == 401
    db_session.expire_all()
    assert db_session.get(MFAMethod, method_id).failed_attempts == 1
    evidence = _denials(db_session)
    assert len(evidence) == 1
    assert evidence[0].entity_type == "mfa_method"
    assert evidence[0].metadata_["reason"] == "invalid_mfa_code"


def test_typed_refusal_after_recovery_spend_rolls_back_before_denial_audit(
    db_session,
    person,
    monkeypatch,
):
    credential = _credential(db_session, person)
    credential_id = credential.id
    _, method_id = _enable_mfa(db_session, person)
    method = db_session.get(MFAMethod, method_id)
    recovery_code = af.generate_mfa_recovery_codes(db_session, method)[0]
    result = AuthFlow.login(
        db_session, "user@example.com", "secret", _make_request(), None
    )

    def refuse(*args, **kwargs):
        raise pa.PasswordAuthenticationError("credential_changed", "Credential changed")

    monkeypatch.setattr(af, "stage_session_issue", refuse)
    with pytest.raises(HTTPException) as refusal:
        AuthFlow.mfa_verify(
            db_session, result["mfa_token"], recovery_code, _make_request()
        )
    assert refusal.value.status_code == 409
    db_session.expire_all()
    assert db_session.scalars(select(AuthSession)).all() == []
    assert all(
        row.used_at is None for row in db_session.scalars(select(MFARecoveryCode)).all()
    )
    method = db_session.get(MFAMethod, method_id)
    assert (method.failed_attempts, method.last_used_at) == (0, None)
    evidence = _denials(db_session)
    assert len(evidence) == 1
    assert evidence[0].entity_id == str(credential_id)
    assert evidence[0].status_code == 409


def test_enrollment_wrong_code_is_independent_counter_and_audit(
    db_session, person, monkeypatch
):
    from tests.subscriber_mfa_owner_support import authenticated_subscriber_enrollment

    enrollment = authenticated_subscriber_enrollment(
        db_session, subscriber_id=person.id
    )
    setup = enrollment.setup(label="synthetic device")
    method_id = setup.method_id
    import pyotp

    monkeypatch.setattr(pyotp.TOTP, "verify", lambda *args, **kwargs: False)
    with pytest.raises(pa.PasswordAuthenticationError) as refusal:
        enrollment.confirm(method_id=method_id, code="synthetic-wrong")
    assert refusal.value.code == "auth.password_authentication.invalid_mfa_code"
    db_session.expire_all()
    assert db_session.get(MFAMethod, method_id).failed_attempts == 1
    evidence = _denials(db_session)
    assert len(evidence) == 1
    assert evidence[0].metadata_["surface"] == "enrollment"


def test_untyped_internal_audit_failure_is_not_a_denial_command(
    db_session, person, monkeypatch
):
    _credential(db_session, person)

    def unavailable(*args, **kwargs):
        raise RuntimeError("synthetic audit unavailable")

    monkeypatch.setattr(pa, "_stage_audit", unavailable)
    with pytest.raises(RuntimeError, match="synthetic audit unavailable"):
        AuthFlow.login(db_session, "user@example.com", "secret", _make_request(), None)
    db_session.expire_all()
    assert _denials(db_session) == []
    assert db_session.scalars(select(AuthSession)).all() == []


@pytest.mark.parametrize("surface", ["profile", "inspector", "password_change"])
def test_current_password_step_up_audits_without_new_lockout_policy(
    db_session,
    monkeypatch,
    surface,
):
    from app.services import web_system_db_inspector, web_system_profiles
    from tests.test_auth_flow import _system_user_with_credential

    user, credential = _system_user_with_credential(db_session, "step-up@example.test")
    credential_id = credential.id
    user_id = str(user.id)
    credential.failed_login_attempts = 3
    db_session.commit()
    if surface == "profile":
        assert (
            web_system_profiles.verify_current_password(
                db_session, system_user_id=user_id, password="synthetic-wrong"
            )
            is False
        )
    elif surface == "inspector":
        assert (
            web_system_db_inspector._verify_local_password(
                db_session, principal_id=user_id, password="synthetic-wrong"
            )
            is False
        )  # noqa: SLF001
    else:
        with pytest.raises(HTTPException) as refusal:
            af.change_password(
                db_session, user_id, "synthetic-wrong", "Synthetic-next-password1!"
            )
        assert refusal.value.status_code == 401
    db_session.expire_all()
    row = db_session.get(UserCredential, credential_id)
    assert (row.failed_login_attempts, row.last_login_at, row.credential_version) == (
        3,
        None,
        1,
    )
    assert len(_denials(db_session)) == 1


def test_vendor_known_nonstaff_preadmission_retains_credential_target(
    db_session, person
):
    from app.api.vendor_auth import vendor_login
    from app.schemas.vendor_auth import VendorLoginRequest

    credential = _credential(db_session, person)
    credential_id = credential.id
    with pytest.raises(HTTPException) as refusal:
        vendor_login(
            payload=VendorLoginRequest(
                username="user@example.com", password="synthetic-wrong"
            ),
            request=_make_request(),
            db=db_session,
        )
    assert refusal.value.status_code == 403
    evidence = _denials(db_session)
    assert len(evidence) == 1
    assert evidence[0].entity_id == str(credential_id)
    assert evidence[0].metadata_["surface"] == "vendor_api"
    db_session.expire_all()
    assert db_session.get(UserCredential, credential_id).failed_login_attempts == 0


@pytest.mark.parametrize(
    "kind, expected_status", [("lock_timeout", 503), ("mfa_locked", 429)]
)
def test_customer_mfa_public_response_and_real_denial_status_match(
    db_session,
    person,
    monkeypatch,
    kind,
    expected_status,
):
    from datetime import UTC, datetime, timedelta

    from app.services import web_customer_auth as customer

    _credential(db_session, person)
    _, method_id = _enable_mfa(db_session, person)
    result = AuthFlow.login(
        db_session, "user@example.com", "secret", _make_request(), None
    )
    subscriber_id = str(person.id)
    monkeypatch.setattr(
        customer,
        "_decode_customer_mfa_context",
        lambda *_args, **_kwargs: {
            "subscriber_id": subscriber_id,
            "account_id": subscriber_id,
            "remember": False,
        },
    )
    if kind == "mfa_locked":
        method = db_session.get(MFAMethod, method_id)
        method.locked_until = datetime.now(UTC) + timedelta(minutes=15)
        db_session.commit()
    else:

        def busy(*args, **kwargs):
            raise pa.PasswordAuthenticationError("lock_timeout", "Try again")

        monkeypatch.setattr(pa, "complete_mfa", busy)
    request = _make_request()
    request.scope["headers"].append(
        (
            b"cookie",
            f"customer_mfa_pending={result['mfa_token']}; customer_mfa_context=synthetic-context".encode(),
        )
    )
    response = customer.customer_mfa_submit(request, db_session, "synthetic-code")
    assert response.status_code == expected_status
    evidence = _denials(db_session)
    assert len(evidence) == 1
    assert evidence[0].status_code == expected_status
    if kind == "lock_timeout":
        assert response.headers["retry-after"] == "5"


def test_empty_profile_password_stays_refused_even_if_verifier_accepts_it(
    db_session, monkeypatch
):
    from app.services import web_system_profiles as profiles
    from tests.test_auth_flow import _system_user_with_credential

    user, credential = _system_user_with_credential(
        db_session, "empty-step-up@example.test"
    )
    user_id = str(user.id)
    credential_id = credential.id
    called: list[bool] = []

    def accept(*args):
        called.append(True)
        return True

    monkeypatch.setattr(profiles, "verify_user_password", accept)
    assert (
        profiles.verify_current_password(
            db_session, system_user_id=user_id, password=""
        )
        is False
    )
    assert called == []
    evidence = _denials(db_session)
    assert len(evidence) == 1 and evidence[0].entity_id == str(credential_id)


def test_known_access_wrong_secret_audits_source_without_local_counter(
    db_session, person
):
    from app.models.catalog import AccessCredential

    local = _credential(db_session, person)
    local_id = local.id
    source = AccessCredential(
        subscriber_id=person.id,
        username="access-only-synthetic",
        secret_hash=af.hash_service_secret("synthetic-access-value"),
        is_active=True,
    )
    db_session.add(source)
    db_session.commit()
    source_id = source.id
    with pytest.raises(HTTPException) as refusal:
        AuthFlow.login(
            db_session,
            "access-only-synthetic",
            "synthetic-wrong",
            _make_request(),
            None,
        )
    assert refusal.value.status_code == 401
    evidence = _denials(db_session)
    assert len(evidence) == 1
    assert (evidence[0].entity_type, evidence[0].entity_id) == (
        "access_credential",
        str(source_id),
    )
    db_session.expire_all()
    assert db_session.get(UserCredential, local_id).failed_login_attempts == 0
    assert db_session.get(AccessCredential, source_id).last_auth_at is None


def test_ambiguous_access_decision_remains_aggregate_only(db_session, monkeypatch):
    outcome = af.AccessCredentialVerificationOutcome(
        af.AccessCredentialVerificationStatus.AMBIGUOUS
    )
    monkeypatch.setattr(
        af, "_resolve_access_credential_match", lambda *_args, **_kwargs: outcome
    )
    with pytest.raises(HTTPException) as refusal:
        AuthFlow.login(
            db_session,
            "ambiguous-access-synthetic",
            "synthetic-wrong",
            _make_request(),
            None,
        )
    assert refusal.value.status_code == 401
    assert _denials(db_session) == []
    query = af.AccessCredentialVerificationQuery(
        identifier="private-identity", password="private-secret"
    )
    assert "private" not in repr(query)


@pytest.mark.parametrize("kind", ["mfa", "mfa_enrollment"])
@pytest.mark.parametrize("party_id", [None, "malformed-party-claim"])
def test_signed_known_staff_challenge_bad_party_is_audited_once(
    db_session,
    monkeypatch,
    kind,
    party_id,
):
    from datetime import UTC, datetime, timedelta

    from app.services import web_auth
    from tests.test_auth_flow import _system_user_with_credential

    user, credential = _system_user_with_credential(
        db_session, "bad-party@example.test"
    )
    credential_id = credential.id
    user_id = str(user.id)
    now = datetime.now(UTC)
    payload = {
        "typ": kind,
        "ver": 2,
        "sub": user_id,
        "principal_id": user_id,
        "principal_type": "system_user",
        "login_aud": "general",
        "src": pa.SRC_USER_CREDENTIAL,
        "cid": str(credential_id),
        "cv": 1,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
    }
    if party_id is not None:
        payload["party_id"] = party_id
    token = af._jwt_encode_token(
        payload, af._jwt_secret(db_session), af._jwt_algorithm(db_session)
    )  # noqa: SLF001
    with pytest.raises(HTTPException) as refusal:
        if kind == "mfa":
            AuthFlow.mfa_verify(db_session, token, "synthetic-code", _make_request())
        else:
            web_auth._mfa_enrollment_binding(db_session, token, request=_make_request())  # noqa: SLF001
    assert refusal.value.status_code == 401
    evidence = _denials(db_session)
    assert len(evidence) == 1
    assert evidence[0].entity_id == str(credential_id)
    assert evidence[0].metadata_["reason"] == "invalid_mfa_token"
