"""Fast SQLite logic and transaction evidence; not PostgreSQL acceptance."""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType, AuditEvent
from app.models.auth import Session as AuthSession
from app.models.auth import SessionStatus
from app.services import session_manager as sm
from app.services.audit_adapter import AuditActor
from app.services.auth_session_refresh import SessionPrincipalType, hash_refresh_token
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext
from tests import oidc_atomic_unit
from tests.support.tx_trace import assert_protected_tx, trace_transactions

db_session = oidc_atomic_unit.db_session
oidc_unit_engine = oidc_atomic_unit.oidc_unit_engine


def _row(principal_id, token):
    return AuthSession(
        id=uuid4(),
        subscriber_id=principal_id,
        token_hash=hash_refresh_token(token),
        status=SessionStatus.active,
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )


@pytest.fixture
def cohort(db_session, person, monkeypatch):
    principal_id = person.id
    current, other = (
        _row(principal_id, "current-bearer"),
        _row(principal_id, "other-bearer"),
    )
    db_session.add_all([current, other])
    db_session.flush()
    ids = current.id, other.id
    db_session.commit()

    @contextmanager
    def fresh():
        with Session(
            bind=db_session.get_bind(), join_transaction_mode="create_savepoint"
        ) as command_db:
            yield command_db

    monkeypatch.setattr(db_session_adapter, "owner_command_session", fresh)
    return principal_id, *ids


def _command(cohort, intent=sm.RevocationIntent.SINGLE, **changes):
    principal_id, current_id, target_id = cohort
    values = dict(
        context=CommandContext.system(
            actor=str(principal_id), scope="test_session", reason="unit_revocation"
        ),
        intent=intent,
        actor=AuditActor.user(str(principal_id)),
        principal_type=SessionPrincipalType.SUBSCRIBER,
        principal_id=principal_id,
        current_session_id=current_id,
        session_id=target_id,
    )
    values.update(changes)
    return sm.RevokeSessionsCommand(**values)


def _execute(command):
    with db_session_adapter.owner_command_session() as db:
        return sm.revoke_authentication_sessions(db, command)


@pytest.mark.parametrize("intent", list(sm.RevocationIntent))
def test_atomic_revocation_actual_audit_and_commit(db_session, cohort, intent):
    command = _command(cohort, intent=intent, refresh_token="other-bearer")
    with trace_transactions(db_session.get_bind().engine) as trace:
        outcome = _execute(command)
    assert_protected_tx(trace.result)
    assert outcome.revoked_ids == (cohort[2],)
    audit = db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == "auth.sessions_revoked")
    ).one()
    assert audit.actor_id == (
        "vendor_admission"
        if intent is sm.RevocationIntent.COMPENSATE
        else str(cohort[0])
    )
    assert audit.actor_type == (
        AuditActorType.system
        if intent is sm.RevocationIntent.COMPENSATE
        else AuditActorType.user
    )
    assert "other-bearer" not in str(audit.metadata_)
    assert hash_refresh_token("other-bearer") not in str(audit.metadata_)


@pytest.mark.parametrize("bad_current", [None, "foreign"])
def test_missing_or_wrong_current_never_revokes_all(db_session, cohort, bad_current):
    command = _command(
        cohort,
        intent=sm.RevocationIntent.OTHERS,
        current_session_id=uuid4() if bad_current else None,
    )
    for _ in range(2):
        with pytest.raises(
            sm.SessionRevocationError, match="current authenticated session"
        ):
            _execute(command)
    db_session.expire_all()
    assert db_session.get(AuthSession, cohort[2]).status == SessionStatus.active


def test_cross_principal_target_never_changes(db_session, cohort):
    command = _command(cohort, principal_id=uuid4())
    for _ in range(2):
        with pytest.raises(sm.SessionRevocationError):
            _execute(command)
    db_session.expire_all()
    assert db_session.get(AuthSession, cohort[2]).status == SessionStatus.active


@pytest.mark.parametrize("failure", ["audit", "flush", "commit"])
def test_failure_rolls_back_without_external_cleanup(
    db_session, cohort, monkeypatch, failure
):
    if failure == "audit":
        target, name = sm, "stage_audit_event"
    else:
        target, name = Session, failure

    def refuse(*args, **kwargs):
        raise RuntimeError("injected failure")

    with monkeypatch.context() as scoped:
        if failure == "commit":
            event.listen(Session, "before_commit", refuse)
        else:
            scoped.setattr(target, name, refuse)
        with trace_transactions(db_session.get_bind().engine) as trace:
            with pytest.raises(RuntimeError, match="injected failure"):
                try:
                    _execute(_command(cohort))
                finally:
                    if failure == "commit":
                        event.remove(Session, "before_commit", refuse)
        assert_protected_tx(trace.result, success=False)
    db_session.expire_all()
    assert db_session.get(AuthSession, cohort[2]).status == SessionStatus.active


def test_repeated_admin_revoke_preserves_timestamp(db_session, cohort):
    command = _command(
        cohort, intent=sm.RevocationIntent.ADMIN, actor=AuditActor.api_key("test-key")
    )
    first = _execute(command)
    second = _execute(command)
    assert second.revoked_ids == ()
    assert first.revoked_at.replace(tzinfo=UTC) == second.revoked_at.replace(tzinfo=UTC)


def test_previous_rotated_bearer_is_not_logout_authority(db_session, cohort):
    row = db_session.get(AuthSession, cohort[2])
    row.previous_token_hash = row.token_hash
    row.token_hash = hash_refresh_token("rotated-bearer")
    db_session.commit()
    with pytest.raises(sm.SessionRevocationError):
        _execute(
            _command(
                cohort, intent=sm.RevocationIntent.LOGOUT, refresh_token="other-bearer"
            )
        )


def test_bearer_command_repr_is_redacted(cohort):
    assert "private-bearer" not in repr(
        _command(cohort, refresh_token="private-bearer")
    )


@pytest.mark.parametrize(
    "transport",
    [
        "bearer",
        "api_logout",
        "self_single",
        "self_others",
        "admin_delete",
        "vendor_compensation",
        "vendor_logout",
        "staff_web",
        "admin_web",
    ],
)
def test_public_transport_actual_audit_transaction(
    db_session, cohort, transport, monkeypatch
):
    from app.api import auth as auth_api
    from app.api import auth_flow as flow_api
    from app.api import vendor_auth
    from app.schemas.auth_flow import LogoutRequest
    from app.services import web_auth
    from app.services.auth_flow import AuthFlow
    from app.web.admin import system
    from tests.test_auth_flow import _make_request

    auth = {
        "principal_id": str(cohort[0]),
        "subscriber_id": str(cohort[0]),
        "principal_type": "subscriber",
        "session_id": str(cohort[1]),
    }
    request = _make_request()
    request._cookies = {"refresh_token": "other-bearer"}
    # Response-only cookie settings use the adapter read session, independently.
    monkeypatch.setattr(
        AuthFlow,
        "refresh_cookie_settings",
        staticmethod(
            lambda db=None: {
                "key": "refresh_token",
                "httponly": True,
                "secure": False,
                "samesite": "lax",
                "domain": None,
                "path": "/",
                "max_age": 100,
            }
        ),
    )
    if transport == "admin_web":
        from app.models.system_user import SystemUser

        staff = SystemUser(
            id=uuid4(),
            first_name="Session",
            last_name="Test",
            email=f"session-{uuid4()}@example.com",
        )
        db_session.add(staff)
        db_session.flush()
        staff_current, staff_other = (
            _row(None, "staff-current"),
            _row(None, "staff-other"),
        )
        staff_current.system_user_id = staff.id
        staff_other.system_user_id = staff.id
        db_session.add_all([staff_current, staff_other])
        db_session.flush()
        auth = {
            "principal_id": str(staff.id),
            "principal_type": "system_user",
            "session_id": str(staff_current.id),
        }
        db_session.commit()
        monkeypatch.setattr(
            system, "_require_system_user_principal", lambda request: auth
        )
    with trace_transactions(db_session.get_bind().engine) as trace:
        if transport == "bearer":
            AuthFlow.logout(db_session, "other-bearer")
        elif transport == "api_logout":
            flow_api.logout(
                LogoutRequest(refresh_token="other-bearer"), request, db_session
            )
        elif transport == "self_single":
            flow_api.revoke_session(str(cohort[2]), auth, db_session)
        elif transport == "self_others":
            flow_api.revoke_all_other_sessions(auth, db_session)
        elif transport == "admin_delete":
            auth_api.delete_session(str(cohort[2]), db_session, auth)
        elif transport == "vendor_compensation":
            vendor_auth._discard_refused_session(db_session, "other-bearer")
        elif transport == "vendor_logout":
            from app.schemas.vendor_auth import VendorLogoutRequest

            vendor_auth.vendor_logout(
                VendorLogoutRequest(refresh_token="other-bearer"), request, db_session
            )
        elif transport == "staff_web":
            web_auth.logout(request, db_session)
        elif transport == "admin_web":
            system.user_profile_sign_out_other_sessions(request, db_session)
    assert_protected_tx(trace.result)


def test_api_key_cannot_use_self_revocation(db_session, cohort):
    from app.api import auth_flow

    with pytest.raises(HTTPException) as exc:
        auth_flow.revoke_all_other_sessions(
            {"principal_type": "api_key", "api_key_id": "key"}, db_session
        )
    assert exc.value.status_code == 403


@pytest.mark.parametrize("kind", list(SessionPrincipalType))
def test_exact_binding_all_principal_kinds(db_session, cohort, kind):
    from app.models.subscriber import ResellerUser
    from app.models.system_user import SystemUser

    if kind is SessionPrincipalType.SUBSCRIBER:
        active_cohort = cohort
    else:
        principal = (
            SystemUser(
                id=uuid4(),
                first_name="Session",
                last_name="Test",
                email=f"{uuid4()}@example.com",
            )
            if kind is SessionPrincipalType.SYSTEM_USER
            else ResellerUser(id=uuid4(), email=f"{uuid4()}@example.com")
        )
        db_session.add(principal)
        db_session.flush()
        rows = [_row(None, f"{kind}-{uuid4()}") for _ in range(2)]
        for row in rows:
            if kind is SessionPrincipalType.SYSTEM_USER:
                row.system_user_id = principal.id
            else:
                row.reseller_user_id = principal.id
        db_session.add_all(rows)
        db_session.flush()
        active_cohort = principal.id, rows[0].id, rows[1].id
        db_session.commit()
    result = _execute(_command(active_cohort, principal_type=kind))
    assert result.principal_type is kind
    assert result.principal_id == active_cohort[0]
    assert result.revoked_ids == (active_cohort[2],)
    with pytest.raises(sm.SessionRevocationError):
        _execute(
            _command(
                cohort,
                session_id=active_cohort[1],
                principal_type=SessionPrincipalType.RESELLER_USER
                if kind is SessionPrincipalType.SUBSCRIBER
                else SessionPrincipalType.SUBSCRIBER,
            )
        )


def test_projection_retries_exact_targets_without_revoking_later_login(
    db_session, cohort, monkeypatch
):
    from app.models.event_store import EventStore
    from app.services import auth_cache
    from app.services.events.handlers.session_revocation_projection import (
        SessionRevocationProjectionHandler,
    )
    from app.services.events.types import Event, EventType

    _execute(_command(cohort))
    stored = db_session.scalars(
        select(EventStore).where(
            EventStore.event_type == EventType.authentication_sessions_revoked.value
        )
    ).one()
    payload = dict(stored.payload)
    later = _row(cohort[0], "later-login")
    db_session.add(later)
    db_session.flush()
    later_id = later.id
    db_session.commit()
    event = Event(event_type=EventType.authentication_sessions_revoked, payload=payload)
    calls = []

    def unavailable(session_id, **kwargs):
        calls.append(session_id)
        raise RuntimeError("redis down")

    monkeypatch.setattr(auth_cache, "invalidate_session_context_strict", unavailable)
    handler = SessionRevocationProjectionHandler()
    with pytest.raises(RuntimeError, match="redis down"):
        handler.handle(db_session, event)
    monkeypatch.setattr(
        auth_cache,
        "invalidate_session_context_strict",
        lambda session_id, **kwargs: calls.append(session_id),
    )
    handler.handle(db_session, event)
    assert calls == [str(cohort[2]), str(cohort[2])]
    assert db_session.get(AuthSession, later_id).status == SessionStatus.active


@pytest.mark.parametrize("transport", ["staff_web", "vendor_compensation"])
def test_failure_adapter_preserves_retry_bearer(
    db_session, cohort, monkeypatch, transport
):
    from app.api import vendor_auth
    from app.services import web_auth
    from app.services.auth_flow import AuthFlow
    from tests.test_auth_flow import _make_request

    request = _make_request()
    monkeypatch.setattr(
        AuthFlow, "resolve_refresh_token", staticmethod(lambda *args: "other-bearer")
    )

    def unavailable(*args, **kwargs):
        raise HTTPException(
            status_code=503, detail="busy", headers={"Retry-After": "5"}
        )

    monkeypatch.setattr(AuthFlow, "logout", unavailable)
    with pytest.raises(HTTPException) as exc:
        if transport == "staff_web":
            web_auth.logout(request, db_session)
        else:
            vendor_auth._discard_refused_session(db_session, "other-bearer")
    assert exc.value.status_code == 503
    assert exc.value.headers == {"Retry-After": "5"}
    assert db_session.get(AuthSession, cohort[2]).status == SessionStatus.active


@pytest.mark.parametrize("transport", ["self", "admin"])
def test_malformed_adapter_target_is_not_500(db_session, cohort, transport):
    from app.api import auth, auth_flow

    identity = {
        "principal_id": str(cohort[0]),
        "principal_type": "subscriber",
        "session_id": str(cohort[1]),
    }
    with pytest.raises(HTTPException) as exc:
        if transport == "self":
            auth_flow.revoke_session("not-a-uuid", identity, db_session)
        else:
            auth.delete_session("not-a-uuid", db_session, identity)
    assert exc.value.status_code == 404


def test_api_key_session_listing_is_typed_refusal(db_session):
    from app.api import auth_flow

    with pytest.raises(HTTPException) as exc:
        auth_flow.list_sessions(
            {"principal_type": "api_key", "api_key_id": "key"}, db_session
        )
    assert exc.value.status_code == 403


def test_failed_logout_does_not_release_caller_flushed_write(db_session, cohort):
    from app.models.subscriber import Subscriber
    from app.services.auth_flow import AuthFlow

    principal = db_session.get(Subscriber, cohort[0])
    original = principal.first_name
    principal.first_name = "Caller-uncommitted-change"
    db_session.flush()
    caller_transaction = db_session.get_transaction()
    assert not db_session.dirty  # A pending-only guard cannot detect this write.
    with pytest.raises(HTTPException) as exc:
        AuthFlow.logout(db_session, "nonexistent-refresh-bearer")
    assert exc.value.status_code == 404
    assert db_session.get_transaction() is caller_transaction
    assert (
        db_session.get(Subscriber, cohort[0]).first_name == "Caller-uncommitted-change"
    )
    with Session(db_session.get_bind()) as observer:
        assert observer.get(Subscriber, cohort[0]).first_name == original
    db_session.rollback()
    assert db_session.get(Subscriber, cohort[0]).first_name == original


def test_admin_api_key_actor_remains_api_key(db_session, cohort):
    from app.api import auth

    with trace_transactions(db_session.get_bind().engine) as trace:
        auth.delete_session(
            str(cohort[2]),
            db_session,
            {"api_key_id": "administrative-test-key", "principal_type": "api_key"},
        )
    assert_protected_tx(trace.result)
    row = db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == "auth.sessions_revoked")
    ).one()
    assert row.actor_type is AuditActorType.api_key
    assert row.actor_id == "administrative-test-key"
    assert row.actor_party_id is None


def test_stale_cache_cannot_restore_revoked_database_standing(
    db_session, cohort, monkeypatch
):
    from app.services import auth_cache
    from app.web.auth import dependencies
    from tests.test_auth_flow import _make_request

    _execute(_command(cohort))
    request = _make_request()
    monkeypatch.setattr(
        dependencies, "get_session_token", lambda request: "test-access-token"
    )
    monkeypatch.setattr(
        dependencies,
        "decode_access_token",
        lambda db, token: {
            "principal_id": str(cohort[0]),
            "principal_type": "subscriber",
            "session_id": str(cohort[2]),
        },
    )
    monkeypatch.setattr(
        auth_cache,
        "get_session_context",
        lambda value: {"principal_id": str(cohort[0]), "roles": ["admin"]},
    )
    monkeypatch.setattr(
        auth_cache, "invalidate_session_context", lambda *args, **kwargs: None
    )
    assert dependencies.validate_session_token(request, db_session) is None


def test_empty_others_is_audited_noop_without_transition_timestamp(db_session, cohort):
    _execute(_command(cohort))
    result = _execute(_command(cohort, intent=sm.RevocationIntent.OTHERS))
    assert result.disposition is sm.RevocationDisposition.NOOP
    assert result.revoked_ids == () and result.revoked_at is None
    noop = db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == "auth.sessions_revocation_noop")
    ).one()
    assert noop.metadata_["disposition"] == "noop"
    from app.models.event_store import EventStore
    from app.services.events.types import EventType

    events = list(
        db_session.scalars(
            select(EventStore).where(
                EventStore.event_type == EventType.authentication_sessions_revoked.value
            )
        )
    )
    assert len(events) == 1


def test_already_revoked_missing_timestamp_is_preserved_as_unknown(db_session, cohort):
    target = db_session.get(AuthSession, cohort[2])
    target.status = SessionStatus.revoked
    target.revoked_at = None
    db_session.commit()
    result = _execute(_command(cohort, intent=sm.RevocationIntent.ADMIN))
    assert (
        result.revoked_at is None
        and result.disposition is sm.RevocationDisposition.NOOP
    )
    db_session.expire_all()
    assert db_session.get(AuthSession, cohort[2]).revoked_at is None


def test_schema_accepts_honest_noop_without_timestamp():
    from app.schemas.auth_flow import SessionRevokeResponse

    assert SessionRevokeResponse(revoked_at=None, revoked_count=0).model_dump() == {
        "revoked_at": None,
        "revoked_count": 0,
    }
