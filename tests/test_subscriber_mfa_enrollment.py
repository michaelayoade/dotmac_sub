"""Non-authoritative SQLite enrollment transition and composed render feedback."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pyotp
import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from sqlalchemy import event, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app import subscriber_mfa_enrollment_http as adapter
from app.models.audit import AuditEvent
from app.models.auth import MFAMethod, MFARecoveryCode, SessionStatus
from app.models.auth import Session as AuthSession
from app.models.event_store import EventStore
from app.services import password_authentication as owner


@pytest.fixture
def enrollment(
    db_session: Session, person, monkeypatch: pytest.MonkeyPatch
) -> owner.SetupSubscriberMfaCommand:
    monkeypatch.setenv("TOTP_ENCRYPTION_KEY", Fernet.generate_key().decode())
    from app.services import events

    real_emit = events.emit_event

    def emit(*args, **kwargs):
        kwargs["dispatch_after_commit"] = False
        return real_emit(*args, **kwargs)

    monkeypatch.setattr(events, "emit_event", emit)
    subscriber_id = person.id
    session_id = uuid4()
    db_session.add(
        AuthSession(
            id=session_id,
            subscriber_id=subscriber_id,
            status=SessionStatus.active,
            token_hash=uuid4().hex,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    db_session.commit()
    proof = owner.SubscriberMfaSessionProof(session_id, subscriber_id, False)
    return owner.SetupSubscriberMfaCommand(
        context=adapter.command_context(proof=proof, reason="Test enrollment"),
        subscriber_id=subscriber_id,
        label="Authenticator app",
        provenance=owner.SubscriberMfaProvenance.CUSTOMER_WEB,
        session=proof,
    )


def confirmation(
    command: owner.SetupSubscriberMfaCommand,
    setup: owner.SubscriberMfaSetupOutcome,
    *,
    code: str | None = None,
    delivery: owner.RecoveryCodeDelivery = owner.RecoveryCodeDelivery.NONE,
) -> owner.ConfirmSubscriberMfaCommand:
    return owner.ConfirmSubscriberMfaCommand(
        context=replace(command.context, command_id=uuid4()),
        subscriber_id=command.subscriber_id,
        method_id=setup.method_id,
        code=code if code is not None else pyotp.TOTP(setup.secret).now(),
        provenance=command.provenance,
        session=command.session,
        recovery_delivery=delivery,
    )


def test_setup_reuses_pending_and_commits_safe_evidence(
    db_session: Session, enrollment: owner.SetupSubscriberMfaCommand
) -> None:
    first = owner.setup_subscriber_mfa(db_session, command=enrollment)
    second = owner.setup_subscriber_mfa(db_session, command=enrollment)
    assert first.method_id == second.method_id
    assert first.secret != second.secret
    assert second.secret not in repr(second)
    audit = db_session.scalars(
        select(AuditEvent).where(AuditEvent.entity_id == str(second.method_id))
    ).all()
    event = db_session.scalar(
        select(EventStore).where(EventStore.subscriber_id == enrollment.subscriber_id)
    )
    assert len(audit) == 2
    assert event is not None
    assert event.payload["invalidate_sessions"] is False
    assert second.secret not in str(event.payload)


@pytest.mark.parametrize("delivery", list(owner.RecoveryCodeDelivery))
def test_confirmation_and_reconfirmation_are_atomic(
    db_session: Session,
    enrollment: owner.SetupSubscriberMfaCommand,
    delivery: owner.RecoveryCodeDelivery,
) -> None:
    setup = owner.setup_subscriber_mfa(db_session, command=enrollment)
    command = confirmation(enrollment, setup, delivery=delivery)
    result = owner.confirm_subscriber_mfa(db_session, command=command)
    assert result.method.enabled and result.method.is_primary
    assert result.method.verified_at is not None
    assert bool(result.recovery_codes) is (
        delivery is owner.RecoveryCodeDelivery.RETURN_ONCE
    )
    assert command.code not in repr(command)
    assert not result.recovery_codes or result.recovery_codes[0] not in repr(result)
    reconfirmation = confirmation(enrollment, setup, delivery=delivery)
    repeated = owner.confirm_subscriber_mfa(db_session, command=reconfirmation)
    assert repeated.method.id == result.method.id


@pytest.mark.parametrize(
    "standing", ["revoked", "expired", "foreign", "impersonation", "actor"]
)
def test_session_proof_is_rechecked_after_lock(
    db_session: Session, enrollment: owner.SetupSubscriberMfaCommand, standing: str
) -> None:
    command = enrollment
    if standing in {"revoked", "expired"}:
        session = db_session.get(AuthSession, command.session.session_id)
        assert session is not None
        if standing == "revoked":
            session.status, session.revoked_at = (
                SessionStatus.revoked,
                datetime.now(UTC),
            )
        else:
            session.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db_session.commit()
    elif standing == "foreign":
        command = replace(
            command, session=replace(command.session, subscriber_id=uuid4())
        )
    elif standing == "impersonation":
        command = replace(
            command, session=replace(command.session, is_impersonation=True)
        )
    else:
        command = replace(
            command, context=replace(command.context, actor=f"subscriber:{uuid4()}")
        )
    with pytest.raises(
        owner.PasswordAuthenticationError, match="Authenticated subscriber"
    ):
        owner.setup_subscriber_mfa(db_session, command=command)
    assert not db_session.in_transaction()
    assert (
        db_session.scalar(
            select(MFAMethod.id).where(
                MFAMethod.subscriber_id == enrollment.subscriber_id
            )
        )
        is None
    )


def test_bad_code_releases_locks_before_failure_owner(
    db_session: Session,
    enrollment: owner.SetupSubscriberMfaCommand,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = owner.setup_subscriber_mfa(db_session, command=enrollment)
    effects: list[owner.AuthenticationDenial] = []

    def record(denial: owner.AuthenticationDenial) -> None:
        assert not db_session.in_transaction()
        effects.append(denial)

    monkeypatch.setattr(owner, "record_authentication_denial", record)
    with pytest.raises(owner.PasswordAuthenticationError) as exc:
        owner.confirm_subscriber_mfa(
            db_session, command=confirmation(enrollment, setup, code="invalid")
        )
    assert exc.value.kind == "invalid_mfa_code"
    assert len(effects) == 1 and isinstance(effects[0].effect, owner.MfaFailureEffect)
    assert effects[0].effect.method_id == setup.method_id


@pytest.mark.parametrize(
    "invalid", ["missing", "foreign", "revoked", "empty", "locked", "unsupported"]
)
def test_unavailable_factor_has_no_counter_effect(
    db_session: Session,
    enrollment: owner.SetupSubscriberMfaCommand,
    monkeypatch: pytest.MonkeyPatch,
    invalid: str,
) -> None:
    setup = owner.setup_subscriber_mfa(db_session, command=enrollment)
    command = confirmation(enrollment, setup)
    method = db_session.get(MFAMethod, setup.method_id)
    assert method is not None
    if invalid == "missing":
        command = replace(command, method_id=uuid4())
    elif invalid == "foreign":
        from app.models.subscriber import Subscriber

        foreign = Subscriber(
            first_name="Foreign",
            last_name="Factor",
            email=f"foreign-{uuid4().hex}@example.test",
        )
        db_session.add(foreign)
        db_session.flush()
        method.subscriber_id = foreign.id
    elif invalid == "unsupported":
        method.method_type = owner.MFAMethodType.sms
    elif invalid == "revoked":
        method.is_active = False
    elif invalid == "empty":
        method.secret = None
    else:
        method.locked_until = datetime.now(UTC) + timedelta(minutes=1)
    db_session.commit()
    effects: list[owner.AuthenticationDenial] = []
    monkeypatch.setattr(owner, "record_authentication_denial", effects.append)
    with pytest.raises(owner.PasswordAuthenticationError):
        owner.confirm_subscriber_mfa(db_session, command=command)
    assert effects == []


def test_recovery_staging_failure_rolls_back_confirmation(
    db_session: Session,
    enrollment: owner.SetupSubscriberMfaCommand,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import auth_flow

    setup = owner.setup_subscriber_mfa(db_session, command=enrollment)

    def fail(*args, **kwargs) -> list[str]:
        raise RuntimeError("participant failed")

    monkeypatch.setattr(auth_flow, "stage_mfa_recovery_codes", fail)
    with pytest.raises(RuntimeError, match="participant failed"):
        owner.confirm_subscriber_mfa(
            db_session,
            command=confirmation(
                enrollment, setup, delivery=owner.RecoveryCodeDelivery.RETURN_ONCE
            ),
        )
    method = db_session.get(MFAMethod, setup.method_id)
    assert method is not None and not method.enabled and method.verified_at is None
    assert (
        db_session.scalar(
            select(MFARecoveryCode.id).where(
                MFARecoveryCode.mfa_method_id == setup.method_id
            )
        )
        is None
    )


def test_adapter_rejects_api_keys_and_missing_proof() -> None:
    with pytest.raises(HTTPException) as exc:
        adapter.session_proof(
            principal_type="api_key",
            principal_id=str(uuid4()),
            session_id=str(uuid4()),
            is_impersonation=False,
        )
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc:
        adapter.session_proof(
            principal_type="subscriber",
            principal_id=str(uuid4()),
            session_id="None",
            is_impersonation=False,
        )
    assert exc.value.status_code == 401


def test_customer_setup_route_composes_real_template(
    db_session: Session,
    enrollment: owner.SetupSubscriberMfaCommand,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.web.customer import routes

    customer = {
        "subscriber_id": str(enrollment.subscriber_id),
        "account_id": str(enrollment.subscriber_id),
        "auth_session_id": str(enrollment.session.session_id),
        "read_only": False,
        "current_user": {
            "name": "Enrollment",
            "initials": "EN",
            "email": "unit@example.test",
        },
    }
    monkeypatch.setattr(
        routes, "get_current_customer_from_request", lambda request, db: customer
    )
    monkeypatch.setattr(
        routes,
        "optional_customer_subscriber_id",
        lambda db, current: enrollment.subscriber_id,
    )
    setup = owner.SubscriberMfaSetupOutcome(
        uuid4(), pyotp.random_base32(), "otpauth://totp/unit", uuid4(), uuid4()
    )
    commands: list[owner.SetupSubscriberMfaCommand] = []

    def stage(
        *, command: owner.SetupSubscriberMfaCommand
    ) -> owner.SubscriberMfaSetupOutcome:
        commands.append(command)
        return setup

    monkeypatch.setattr(adapter, "setup", stage)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/portal/profile/mfa/setup",
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("test", 80),
        }
    )
    response = routes.customer_mfa_setup(request=request, db=db_session)
    assert response.status_code == 200
    assert setup.secret.encode() in response.body
    assert commands[0].session.session_id == enrollment.session.session_id


@pytest.mark.parametrize("boundary", ["audit", "event", "commit"])
def test_confirmation_evidence_failure_rolls_back_everything(
    db_session: Session,
    enrollment: owner.SetupSubscriberMfaCommand,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    from app.services import events

    setup = owner.setup_subscriber_mfa(db_session, command=enrollment)

    def fail(*args, **kwargs) -> None:
        raise RuntimeError("injected owner completion failure")

    if boundary == "audit":
        monkeypatch.setattr(owner, "stage_audit_event", fail)
    elif boundary == "event":
        monkeypatch.setattr(events, "emit_event", fail)
    else:
        event.listen(db_session, "before_commit", fail)
    with pytest.raises(RuntimeError, match="injected owner completion failure"):
        owner.confirm_subscriber_mfa(
            db_session,
            command=confirmation(
                enrollment, setup, delivery=owner.RecoveryCodeDelivery.RETURN_ONCE
            ),
        )
    if boundary == "commit":
        event.remove(db_session, "before_commit", fail)
    assert not db_session.in_transaction()
    method = db_session.get(MFAMethod, setup.method_id)
    assert method is not None and not method.enabled and not method.is_primary
    assert method.verified_at is None
    assert (
        db_session.scalar(
            select(MFARecoveryCode.id).where(MFARecoveryCode.mfa_method_id == method.id)
        )
        is None
    )
    assert (
        len(
            db_session.scalars(
                select(AuditEvent).where(AuditEvent.entity_id == str(method.id))
            ).all()
        )
        == 1
    )
    assert (
        len(
            db_session.scalars(
                select(EventStore).where(
                    EventStore.subscriber_id == enrollment.subscriber_id
                )
            ).all()
        )
        == 1
    )


@pytest.mark.parametrize(
    "standing", ["revoked", "expired", "foreign", "impersonation", "actor"]
)
def test_confirmation_rechecks_session_proof(
    db_session: Session,
    enrollment: owner.SetupSubscriberMfaCommand,
    standing: str,
) -> None:
    setup = owner.setup_subscriber_mfa(db_session, command=enrollment)
    command = confirmation(enrollment, setup)
    if standing in {"revoked", "expired"}:
        session = db_session.get(AuthSession, command.session.session_id)
        assert session is not None
        if standing == "revoked":
            session.revoked_at = datetime.now(UTC)
        else:
            session.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db_session.commit()
    elif standing == "foreign":
        command = replace(
            command, session=replace(command.session, subscriber_id=uuid4())
        )
    elif standing == "impersonation":
        command = replace(
            command, session=replace(command.session, is_impersonation=True)
        )
    else:
        command = replace(
            command, context=replace(command.context, actor=f"subscriber:{uuid4()}")
        )
    with pytest.raises(owner.PasswordAuthenticationError) as exc:
        owner.confirm_subscriber_mfa(db_session, command=command)
    assert exc.value.kind == "invalid_mfa_session"
    method = db_session.get(MFAMethod, setup.method_id)
    assert method is not None and not method.enabled


@pytest.mark.parametrize("change", ["revoked", "foreign"])
def test_late_enrollment_counter_effect_rechecks_factor_standing(
    db_session: Session,
    enrollment: owner.SetupSubscriberMfaCommand,
    change: str,
) -> None:
    setup = owner.setup_subscriber_mfa(db_session, command=enrollment)
    method = db_session.get(MFAMethod, setup.method_id)
    assert method is not None
    if change == "revoked":
        method.is_active, method.secret = False, None
    db_session.commit()
    changed = db_session.execute(
        owner.mfa_failure_statement(
            setup.method_id,
            now=datetime.now(UTC),
            policy=owner.FailurePolicy(5, 15),
            subscriber_id=enrollment.subscriber_id if change == "revoked" else uuid4(),
        )
    ).first()
    assert changed is None
    db_session.expire_all()
    method = db_session.get(MFAMethod, setup.method_id)
    assert method is not None and method.failed_attempts == 0


@pytest.mark.parametrize(
    "surface", ["auth_api", "reseller_api", "customer_web", "reseller_web"]
)
@pytest.mark.parametrize("action", ["setup", "confirm"])
@pytest.mark.parametrize("invalid", ["missing_session", "impersonation"])
def test_every_adapter_rejects_missing_or_impersonated_session(
    db_session: Session,
    enrollment: owner.SetupSubscriberMfaCommand,
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
    action: str,
    invalid: str,
) -> None:
    from types import SimpleNamespace

    from app.api import auth_flow as auth_api
    from app.api import reseller as reseller_api
    from app.schemas.auth_flow import MfaConfirmRequest, MfaSetupRequest
    from app.services import web_reseller_routes
    from app.web.customer import routes

    session_id = (
        str(enrollment.session.session_id) if invalid == "impersonation" else None
    )
    impersonation = invalid == "impersonation"
    principal = {
        "principal_type": "subscriber",
        "principal_id": str(enrollment.subscriber_id),
        "subscriber_id": str(enrollment.subscriber_id),
        "session_id": session_id,
        "impersonated_by": str(uuid4()) if impersonation else None,
    }
    current_user = {
        "name": "Enrollment",
        "initials": "EN",
        "email": "unit@example.test",
    }
    customer = {
        "subscriber_id": str(enrollment.subscriber_id),
        "account_id": str(enrollment.subscriber_id),
        "auth_session_id": session_id,
        "is_impersonation": impersonation,
        "read_only": False,
        "current_user": current_user,
    }
    context = {
        "principal_type": "subscriber",
        "principal_id": str(enrollment.subscriber_id),
        "session": {"auth_session_id": session_id},
        "is_impersonation": impersonation,
        "current_user": current_user,
        "reseller": SimpleNamespace(id=uuid4(), name="Reseller"),
    }
    monkeypatch.setattr(
        routes, "get_current_customer_from_request", lambda request, db: customer
    )
    monkeypatch.setattr(
        routes,
        "optional_customer_subscriber_id",
        lambda db, current: enrollment.subscriber_id,
    )
    monkeypatch.setattr(
        web_reseller_routes, "_require_reseller_context", lambda request, db: context
    )
    monkeypatch.setattr(
        reseller_api, "_reseller_id", lambda db, principal: str(uuid4())
    )

    def unexpected(*args, **kwargs):
        pytest.fail("invalid proof reached an enrollment command")

    monkeypatch.setattr(adapter, "setup", unexpected)
    monkeypatch.setattr(adapter, "confirm", unexpected)
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/profile/mfa",
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("test", 80),
        }
    )

    def invoke():
        if surface == "auth_api":
            if action == "setup":
                return auth_api.mfa_setup(
                    payload=MfaSetupRequest(subscriber_id=enrollment.subscriber_id),
                    auth=principal,
                    db=db_session,
                )
            return auth_api.mfa_confirm(
                payload=MfaConfirmRequest(method_id=uuid4(), code="123456"),
                auth=principal,
                db=db_session,
            )
        if surface == "reseller_api":
            if action == "setup":
                return reseller_api.my_reseller_mfa_setup(
                    db=db_session, principal=principal
                )
            return reseller_api.my_reseller_mfa_confirm(
                payload=reseller_api.MfaConfirmRequest(
                    method_id=str(uuid4()), code="123456"
                ),
                db=db_session,
                principal=principal,
            )
        if surface == "customer_web":
            if action == "setup":
                return routes.customer_mfa_setup(request=request, db=db_session)
            return routes.customer_mfa_confirm(
                request=request, method_id=str(uuid4()), code="123456", db=db_session
            )
        if action == "setup":
            return web_reseller_routes.reseller_mfa_setup(
                request=request, db=db_session
            )
        return web_reseller_routes.reseller_mfa_confirm(
            request=request, db=db_session, method_id=str(uuid4()), code="123456"
        )

    expected = 403 if impersonation else 401
    if surface.endswith("web") and action == "confirm":
        response = invoke()
        assert response.status_code == expected
    else:
        with pytest.raises(HTTPException) as exc:
            invoke()
        assert exc.value.status_code == expected


@pytest.mark.parametrize("principal_type", ["api_key", "system_user", "reseller_user"])
@pytest.mark.parametrize("action", ["setup", "confirm"])
def test_auth_api_rejects_other_principals(
    db_session: Session,
    enrollment: owner.SetupSubscriberMfaCommand,
    principal_type: str,
    action: str,
) -> None:
    from app.api import auth_flow as auth_api
    from app.schemas.auth_flow import MfaConfirmRequest, MfaSetupRequest

    auth = {
        "principal_type": principal_type,
        "principal_id": str(enrollment.subscriber_id),
        "subscriber_id": str(enrollment.subscriber_id),
        "session_id": str(enrollment.session.session_id),
    }
    with pytest.raises(HTTPException) as exc:
        if action == "setup":
            auth_api.mfa_setup(
                payload=MfaSetupRequest(subscriber_id=enrollment.subscriber_id),
                auth=auth,
                db=db_session,
            )
        else:
            auth_api.mfa_confirm(
                payload=MfaConfirmRequest(method_id=uuid4(), code="123456"),
                auth=auth,
                db=db_session,
            )
    assert exc.value.status_code == 403


def test_switching_primary_preserves_other_verified_factor(
    db_session: Session,
    enrollment: owner.SetupSubscriberMfaCommand,
) -> None:
    first = owner.setup_subscriber_mfa(db_session, command=enrollment)
    owner.confirm_subscriber_mfa(db_session, command=confirmation(enrollment, first))
    second = owner.setup_subscriber_mfa(db_session, command=enrollment)
    assert first.method_id != second.method_id
    result = owner.confirm_subscriber_mfa(
        db_session, command=confirmation(enrollment, second)
    )
    previous = db_session.get(MFAMethod, first.method_id)
    assert previous is not None and previous.enabled and not previous.is_primary
    assert result.method.is_primary


@pytest.mark.parametrize("boundary", ["audit", "event", "commit"])
def test_setup_evidence_failure_discards_pending_factor(
    db_session: Session,
    enrollment: owner.SetupSubscriberMfaCommand,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    from app.services import events

    def fail(*args, **kwargs) -> None:
        raise RuntimeError("injected setup failure")

    if boundary == "audit":
        monkeypatch.setattr(owner, "stage_audit_event", fail)
    elif boundary == "event":
        monkeypatch.setattr(events, "emit_event", fail)
    else:
        event.listen(db_session, "before_commit", fail)
    try:
        with pytest.raises(RuntimeError, match="injected setup failure"):
            owner.setup_subscriber_mfa(db_session, command=enrollment)
    finally:
        if boundary == "commit":
            event.remove(db_session, "before_commit", fail)
    assert not db_session.in_transaction()
    assert (
        db_session.scalar(
            select(MFAMethod.id).where(
                MFAMethod.subscriber_id == enrollment.subscriber_id
            )
        )
        is None
    )
    assert (
        db_session.scalar(
            select(EventStore.id).where(
                EventStore.subscriber_id == enrollment.subscriber_id
            )
        )
        is None
    )


def test_wrong_code_commits_counter_and_audit_on_independent_connection(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """File SQLite proves independent failure lifecycle, never PostgreSQL locking."""
    from collections.abc import Iterator
    from contextlib import contextmanager

    from sqlalchemy import create_engine

    from app.models.subscriber import Subscriber
    from app.services import auth_flow

    database = create_engine(f"sqlite+pysqlite:///{tmp_path / 'enrollment.sqlite'}")
    for model in (Subscriber, AuthSession, MFAMethod, AuditEvent):
        model.__table__.create(database)
    monkeypatch.setenv("TOTP_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(
        auth_flow, "_mfa_failure_policy", lambda db: owner.FailurePolicy(5, 15)
    )

    @contextmanager
    def independent_session() -> Iterator[Session]:
        with Session(database) as db:
            yield db

    monkeypatch.setattr(
        owner.db_session_adapter, "owner_command_session", independent_session
    )
    subscriber_id, session_id, method_id = uuid4(), uuid4(), uuid4()
    with Session(database) as seed:
        seed.add(
            Subscriber(
                id=subscriber_id,
                first_name="Counter",
                last_name="Evidence",
                email="counter@example.test",
            )
        )
        seed.add(
            AuthSession(
                id=session_id,
                subscriber_id=subscriber_id,
                token_hash=uuid4().hex,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        seed.add(
            MFAMethod(
                id=method_id,
                subscriber_id=subscriber_id,
                method_type=owner.MFAMethodType.totp,
                enabled=False,
                is_active=True,
                secret=auth_flow._encrypt_secret(seed, pyotp.random_base32()),
            )
        )
        seed.commit()
    proof = owner.SubscriberMfaSessionProof(session_id, subscriber_id, False)
    with Session(database) as caller:
        real_record = owner.record_authentication_denial

        def record(denial: owner.AuthenticationDenial) -> None:
            assert not caller.in_transaction()
            real_record(denial)

        monkeypatch.setattr(owner, "record_authentication_denial", record)
        with pytest.raises(owner.PasswordAuthenticationError) as exc:
            owner.confirm_subscriber_mfa(
                caller,
                command=owner.ConfirmSubscriberMfaCommand(
                    context=adapter.command_context(
                        proof=proof, reason="Independent counter evidence"
                    ),
                    subscriber_id=subscriber_id,
                    method_id=method_id,
                    code="invalid",
                    provenance=owner.SubscriberMfaProvenance.CUSTOMER_WEB,
                    session=proof,
                    recovery_delivery=owner.RecoveryCodeDelivery.NONE,
                ),
            )
        assert exc.value.kind == "invalid_mfa_code"
        caller.rollback()
    with Session(database) as observer:
        method = observer.get(MFAMethod, method_id)
        assert method is not None and method.failed_attempts == 1 and not method.enabled
        audit = observer.scalar(
            select(AuditEvent).where(AuditEvent.entity_id == str(method_id))
        )
        assert audit is not None and not audit.is_success
    database.dispose()


def test_native_reseller_api_reports_its_confirmed_factor(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import reseller as reseller_api
    from app.models.subscriber import Reseller, ResellerUser

    monkeypatch.setenv("TOTP_ENCRYPTION_KEY", Fernet.generate_key().decode())
    reseller = Reseller(name="Native reseller")
    db_session.add(reseller)
    db_session.flush()
    user = ResellerUser(
        reseller_id=reseller.id,
        subscriber_id=None,
        email="native.mfa@example.test",
        full_name="Native MFA",
        is_active=True,
    )
    db_session.add(user)
    db_session.flush()
    user_id = user.id
    db_session.commit()
    principal = {
        "principal_type": "reseller_user",
        "principal_id": str(user_id),
        "subscriber_id": str(user_id),
    }
    setup = reseller_api.my_reseller_mfa_setup(db=db_session, principal=principal)
    confirmed = reseller_api.my_reseller_mfa_confirm(
        db=db_session,
        principal=principal,
        payload=reseller_api.MfaConfirmRequest(
            method_id=setup["method_id"], code=pyotp.TOTP(setup["secret"]).now()
        ),
    )
    assert confirmed["mfa_enabled"] is True
    assert confirmed["mfa_methods"] == [
        {
            "id": setup["method_id"],
            "label": "Authenticator app",
            "method_type": "totp",
            "verified_at": confirmed["mfa_methods"][0]["verified_at"],
            "enabled": True,
        }
    ]
    assert confirmed["mfa_methods"][0]["verified_at"] is not None
    assert "secret" not in confirmed["mfa_methods"][0]
    summary = owner.query_principal_mfa(
        db_session,
        query=owner.PrincipalMfaQuery(owner.MfaProfilePrincipal.RESELLER_USER, user_id),
    )
    assert summary.enabled and summary.methods[0].id == UUID(setup["method_id"])


def test_mfa_projection_distinguishes_principal_kind_and_omits_secrets(
    db_session: Session,
    enrollment: owner.SetupSubscriberMfaCommand,
) -> None:
    from dataclasses import asdict

    from app.models.subscriber import ResellerUser

    pending = owner.setup_subscriber_mfa(db_session, command=enrollment)
    db_session.add(ResellerUser(id=enrollment.subscriber_id, is_active=True))
    native = MFAMethod(
        reseller_user_id=enrollment.subscriber_id,
        method_type=owner.MFAMethodType.totp,
        enabled=True,
        verified_at=datetime.now(UTC),
    )
    db_session.add(native)
    db_session.flush()
    native_id = native.id
    db_session.commit()
    subscriber = owner.query_principal_mfa(
        db_session,
        query=owner.PrincipalMfaQuery(
            owner.MfaProfilePrincipal.SUBSCRIBER, enrollment.subscriber_id
        ),
    )
    reseller = owner.query_principal_mfa(
        db_session,
        query=owner.PrincipalMfaQuery(
            owner.MfaProfilePrincipal.RESELLER_USER, enrollment.subscriber_id
        ),
    )
    assert [method.id for method in subscriber.methods] == [pending.method_id]
    assert not subscriber.enabled
    assert [method.id for method in reseller.methods] == [
        native_id
    ] and reseller.enabled
    assert set(asdict(reseller.methods[0])) == {
        "id",
        "label",
        "method_type",
        "verified_at",
        "enabled",
    }
    native.is_active = False
    db_session.commit()
    assert (
        owner.query_principal_mfa(
            db_session,
            query=owner.PrincipalMfaQuery(
                owner.MfaProfilePrincipal.RESELLER_USER, enrollment.subscriber_id
            ),
        ).methods
        == ()
    )


@pytest.mark.parametrize("surface", ["customer", "reseller"])
def test_subscriber_web_confirmation_renders_only_committed_recovery_tuple(
    db_session: Session,
    enrollment: owner.SetupSubscriberMfaCommand,
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
) -> None:
    from types import SimpleNamespace

    from app.services import auth_flow, web_reseller_routes
    from app.web.customer import routes

    setup = owner.setup_subscriber_mfa(db_session, command=enrollment)
    outcome = owner.confirm_subscriber_mfa(
        db_session,
        command=confirmation(
            enrollment, setup, delivery=owner.RecoveryCodeDelivery.RETURN_ONCE
        ),
    )
    commands: list[owner.ConfirmSubscriberMfaCommand] = []

    def confirm(
        *, command: owner.ConfirmSubscriberMfaCommand
    ) -> owner.SubscriberMfaConfirmationOutcome:
        commands.append(command)
        return outcome

    monkeypatch.setattr(adapter, "confirm", confirm)

    def forbidden(*args, **kwargs):
        pytest.fail("subscriber route issued recovery codes after owner commit")

    monkeypatch.setattr(auth_flow, "generate_mfa_recovery_codes", forbidden)
    current_user = {
        "name": "Enrollment",
        "initials": "EN",
        "email": "unit@example.test",
    }
    customer = {
        "subscriber_id": str(enrollment.subscriber_id),
        "account_id": str(enrollment.subscriber_id),
        "auth_session_id": str(enrollment.session.session_id),
        "read_only": False,
        "current_user": current_user,
    }
    monkeypatch.setattr(
        routes, "get_current_customer_from_request", lambda request, db: customer
    )
    monkeypatch.setattr(
        routes,
        "optional_customer_subscriber_id",
        lambda db, current: enrollment.subscriber_id,
    )
    context = {
        "principal_type": "subscriber",
        "principal_id": str(enrollment.subscriber_id),
        "session": {"auth_session_id": str(enrollment.session.session_id)},
        "is_impersonation": False,
        "current_user": current_user,
        "reseller": SimpleNamespace(id=uuid4(), name="Reseller"),
    }
    monkeypatch.setattr(
        web_reseller_routes, "_require_reseller_context", lambda request, db: context
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/profile/mfa/confirm",
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("test", 80),
        }
    )
    if surface == "customer":
        response = routes.customer_mfa_confirm(
            request=request,
            method_id=str(setup.method_id),
            code="123456",
            db=db_session,
        )
    else:
        response = web_reseller_routes.reseller_mfa_confirm(
            request=request,
            db=db_session,
            method_id=str(setup.method_id),
            code="123456",
        )
    assert response.status_code == 200
    assert all(code.encode() in response.body for code in outcome.recovery_codes)
    assert len(commands) == 1
    assert commands[0].recovery_delivery is owner.RecoveryCodeDelivery.RETURN_ONCE
    assert commands[0].session == enrollment.session
