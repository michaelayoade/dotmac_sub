"""Fast, non-authoritative administrative customer MFA reset evidence."""

import json
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from starlette.requests import Request

from app.models.audit import AuditEvent
from app.models.auth import (
    AuthProvider,
    MFAMethod,
    MFAMethodType,
    MFARecoveryCode,
    SessionStatus,
    UserCredential,
)
from app.models.auth import Session as AuthSession
from app.models.event_store import EventStore
from app.models.subscriber import Subscriber, SubscriberCategory, SubscriberStatus
from app.models.system_user import SystemUser
from app.services import credential_administration as owner
from app.services import web_customer_user_access as web
from app.services.owner_commands import CommandContext


@pytest.fixture
def reset_account(db_session, subscriber):
    actor = SystemUser(
        first_name="Support", last_name="Staff", email=f"staff-{uuid4()}@example.test"
    )
    method = MFAMethod(
        subscriber_id=subscriber.id,
        method_type=MFAMethodType.totp,
        enabled=True,
        is_active=True,
        is_primary=True,
        secret="synthetic-seed",
        failed_attempts=3,
        locked_until=datetime(2099, 1, 1, tzinfo=UTC),
    )
    db_session.add_all([actor, method])
    db_session.flush()
    code = MFARecoveryCode(
        mfa_method_id=method.id, code_hash=f"synthetic-code-{uuid4()}"
    )
    session = AuthSession(
        subscriber_id=subscriber.id,
        token_hash=f"synthetic-session-{uuid4()}",
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
    )
    credential = UserCredential(
        subscriber_id=subscriber.id,
        provider=AuthProvider.local,
        username=f"reset-{uuid4()}",
        password_hash="synthetic-no-native-hash",
        credential_version=4,
    )
    db_session.add_all([code, session, credential])
    db_session.flush()
    result = SimpleNamespace(
        subscriber_id=subscriber.id,
        actor_id=actor.id,
        method_id=method.id,
        code_id=code.id,
        session_id=session.id,
        credential_id=credential.id,
    )
    db_session.commit()
    return result


def command(account, **kwargs) -> owner.ResetCustomerMfaCommand:
    context = CommandContext.system(
        actor=str(account.actor_id),
        scope=owner.CUSTOMER_MFA_RESET_SCOPE,
        reason=owner.CUSTOMER_MFA_RESET_REASON,
    )
    return replace(
        owner.ResetCustomerMfaCommand(
            context,
            account.subscriber_id,
            owner.CustomerKind.person,
            owner.AdminCustomerRecovery(account.actor_id),
        ),
        **kwargs,
    )


def test_reset_cleans_revokes_and_redacts_evidence(db_session, reset_account):
    account = reset_account
    result = web.reset_customer_mfa(db_session, command=command(account))
    assert (
        result.changed,
        result.factors_disabled,
        result.factors_cleaned,
        result.recovery_codes_deactivated,
        result.sessions_revoked,
    ) == (True, 1, 1, 1, 1)
    db_session.expire_all()
    method = db_session.get(MFAMethod, account.method_id)
    assert not method.enabled and not method.is_primary and not method.is_active
    assert (
        method.secret is None
        and method.failed_attempts == 0
        and method.locked_until is None
    )
    assert not db_session.get(MFARecoveryCode, account.code_id).is_active
    assert (
        db_session.get(AuthSession, account.session_id).status is SessionStatus.revoked
    )
    assert db_session.get(UserCredential, account.credential_id).credential_version == 4
    audit = db_session.query(AuditEvent).one()
    event = db_session.query(EventStore).one()
    assert audit.action == "auth.mfa_disabled"
    assert audit.actor_id == str(account.actor_id)
    assert event.event_type == "credential_administration.completed"
    evidence = repr(audit.metadata_) + repr(event.payload)
    assert "synthetic-seed" not in evidence and "synthetic-code" not in evidence
    assert "password_hash" not in evidence and "email" not in evidence
    assert str(result.command_id) in evidence and str(result.correlation_id) in evidence


def test_clean_repeat_audits_only_and_preserves_new_session(db_session, reset_account):
    owner.reset_customer_mfa(db_session, command=command(reset_account))
    new_session = AuthSession(
        subscriber_id=reset_account.subscriber_id,
        token_hash=f"new-session-{uuid4()}",
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
    )
    db_session.add(new_session)
    db_session.flush()
    identity = new_session.id
    db_session.commit()
    again = owner.reset_customer_mfa(db_session, command=command(reset_account))
    assert not again.changed and again.sessions_revoked == 0
    assert db_session.query(AuditEvent).count() == 2
    assert db_session.query(EventStore).count() == 1
    assert db_session.get(AuthSession, identity).status is SessionStatus.active


@pytest.mark.parametrize("residue", ["seed", "code", "counter", "lockout"])
def test_legacy_cleanup_is_security_transition(db_session, reset_account, residue):
    method = db_session.get(MFAMethod, reset_account.method_id)
    method.enabled = method.is_primary = method.is_active = False
    method.secret = "synthetic-seed" if residue == "seed" else None
    method.failed_attempts = 1 if residue == "counter" else 0
    method.locked_until = (
        datetime(2099, 1, 1, tzinfo=UTC) if residue == "lockout" else None
    )
    db_session.get(MFARecoveryCode, reset_account.code_id).is_active = residue == "code"
    db_session.commit()
    result = owner.reset_customer_mfa(db_session, command=command(reset_account))
    assert (
        result.changed and result.factors_disabled == 0 and result.sessions_revoked == 1
    )
    assert result.factors_cleaned == (0 if residue == "code" else 1)
    assert result.recovery_codes_deactivated == (1 if residue == "code" else 0)
    assert db_session.query(EventStore).count() == 1


@pytest.mark.parametrize("failure", ["audit", "event"])
def test_evidence_failure_rolls_back_everything(
    db_session, reset_account, monkeypatch, failure
):
    from app.services import audit_adapter, events

    target, name = (
        (audit_adapter, "stage_audit_event")
        if failure == "audit"
        else (events, "emit_event")
    )

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic-evidence-failure")

    monkeypatch.setattr(target, name, fail)
    with pytest.raises(RuntimeError, match="synthetic-evidence-failure"):
        owner.reset_customer_mfa(db_session, command=command(reset_account))
    assert not db_session.in_transaction()
    db_session.expire_all()
    assert db_session.get(MFAMethod, reset_account.method_id).secret == "synthetic-seed"
    assert db_session.get(MFARecoveryCode, reset_account.code_id).is_active
    assert (
        db_session.get(AuthSession, reset_account.session_id).status
        is SessionStatus.active
    )
    assert (
        db_session.query(AuditEvent).count()
        == db_session.query(EventStore).count()
        == 0
    )


@pytest.mark.parametrize("bad", ["actor", "scope", "reason", "provenance", "kind"])
def test_typed_admission_rejects_mismatched_context(db_session, reset_account, bad):
    cmd = command(reset_account)
    if bad == "actor":
        cmd = replace(cmd, context=replace(cmd.context, actor=str(uuid4())))
    elif bad == "scope":
        cmd = replace(cmd, context=replace(cmd.context, scope=owner.SCOPE))
    elif bad == "reason":
        cmd = replace(
            cmd, context=replace(cmd.context, reason="untrusted submitted reason")
        )
    elif bad == "provenance":
        cmd = replace(cmd, provenance=None)
    else:
        cmd = replace(cmd, customer_kind="person")
    with pytest.raises(owner.CredentialAdministrationError, match="Trusted"):
        owner.reset_customer_mfa(db_session, command=cmd)
    assert not db_session.in_transaction()


@pytest.mark.parametrize(
    "condition",
    ["canceled", "no_email", "wrong_business", "inactive_actor", "missing_actor"],
)
def test_target_and_actor_eligibility_fail_closed(db_session, reset_account, condition):
    cmd = command(reset_account)
    subscriber = db_session.get(Subscriber, reset_account.subscriber_id)
    if condition == "canceled":
        subscriber.status = SubscriberStatus.canceled
    elif condition == "no_email":
        subscriber.email = " "
    elif condition == "wrong_business":
        subscriber.category = SubscriberCategory.residential
        cmd = replace(cmd, customer_kind=owner.CustomerKind.business)
    elif condition == "inactive_actor":
        db_session.get(SystemUser, reset_account.actor_id).is_active = False
    else:
        actor_id = uuid4()
        cmd = replace(
            cmd,
            provenance=owner.AdminCustomerRecovery(actor_id),
            context=replace(cmd.context, actor=str(actor_id)),
        )
    db_session.commit()
    with pytest.raises(owner.CredentialAdministrationError):
        owner.reset_customer_mfa(db_session, command=cmd)
    assert db_session.get(MFAMethod, reset_account.method_id).enabled
    assert db_session.query(AuditEvent).count() == 0


def test_flushed_caller_transaction_is_rejected(db_session, reset_account):
    method = db_session.get(MFAMethod, reset_account.method_id)
    method.label = "caller-owned change"
    db_session.flush()
    with pytest.raises(owner.CredentialAdministrationError, match="transaction-free"):
        owner.reset_customer_mfa(db_session, command=command(reset_account))
    assert db_session.in_transaction()
    db_session.rollback()


def request(auth):
    result = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/admin/customers/person/test/user/reset-mfa",
            "headers": [(b"hx-request", b"true")],
        }
    )
    result.state.auth = auth
    return result


def test_adapter_composed_toast_and_trusted_actor(
    db_session, reset_account, monkeypatch
):
    from app.web.admin import customers

    @contextmanager
    def session():
        yield db_session

    monkeypatch.setattr(customers.db_session_adapter, "owner_command_session", session)
    response = customers.customer_user_reset_mfa(
        request=request(
            {
                "principal_type": "system_user",
                "principal_id": str(reset_account.actor_id),
                "subscriber_id": str(uuid4()),
            }
        ),
        customer_type="person",
        customer_id=str(reset_account.subscriber_id),
        db=None,
    )
    toast = json.loads(response.headers["HX-Trigger"])["showToast"]
    assert response.status_code == 204 and toast["type"] == "success"
    assert db_session.query(AuditEvent).one().actor_id == str(reset_account.actor_id)


@pytest.mark.parametrize(
    "auth",
    [
        {},
        {"principal_type": "subscriber", "principal_id": str(uuid4())},
        {"principal_type": "system_user", "principal_id": "bad"},
        {
            "principal_type": "system_user",
            "principal_id": str(uuid4()),
            "impersonated_by": "staff",
        },
    ],
)
def test_adapter_refuses_untrusted_actor(auth):
    from app.web.admin import customers

    response = customers.customer_user_reset_mfa(
        request=request(auth), customer_type="person", customer_id=str(uuid4()), db=None
    )
    assert json.loads(response.headers["HX-Trigger"])["showToast"]["type"] == "error"


def test_adapter_unexpected_failure_has_safe_error_toast(monkeypatch, reset_account):
    from app.web.admin import customers

    def fail(*args, **kwargs):
        raise RuntimeError("private-synthetic-database-detail")

    monkeypatch.setattr(customers.db_session_adapter, "owner_command_session", fail)
    response = customers.customer_user_reset_mfa(
        request=request(
            {
                "principal_type": "system_user",
                "principal_id": str(reset_account.actor_id),
            }
        ),
        customer_type="person",
        customer_id=str(reset_account.subscriber_id),
        db=None,
    )
    rendered = response.headers["HX-Trigger"]
    assert "private-synthetic" not in rendered
    assert json.loads(rendered)["showToast"]["type"] == "error"
