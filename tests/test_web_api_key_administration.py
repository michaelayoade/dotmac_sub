"""Fast SQLite evidence only; hosted migrated PostgreSQL owns lock acceptance."""

from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value
from starlette.requests import Request

from app.models.audit import AuditEvent
from app.models.auth import ApiKey
from app.models.system_user import SystemUser
from app.services import api_key_administration as owner
from app.services.audit_adapter import AuditActor
from app.services.auth import hash_api_key
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import OwnerCommandError
from app.web.admin import system
from tests.support.tx_trace import assert_protected_tx, trace_transactions
from tests.web_api_key_fixtures import create_command, mutate_command


def key_fixture(db: Session, subscriber_id=None, *, active=True, revoked=False):
    past = datetime.now(UTC) - timedelta(days=2)
    key = ApiKey(
        key_hash="inert-fixture-hash",
        subscriber_id=subscriber_id,
        label="excluded-label",
        scopes=["audit:read", "unknown:*"],
        expires_at=past,
        is_active=active,
        revoked_at=past if revoked else None,
    )
    db.add(key)
    db.flush()
    key_id = key.id
    db.commit()
    return key_id


def audit_rows(db: Session, key_id):
    return list(
        db.scalars(select(AuditEvent).where(AuditEvent.entity_id == str(key_id)))
    )


@pytest.mark.parametrize("action", ["create", "revoke", "rotate"])
@pytest.mark.parametrize("api_actor", [False, True])
def test_state_and_canonical_audit_share_root(db_session, action, api_actor):
    if action == "create":
        command = create_command(
            subscriber_id=None,
            label="excluded-label",
            expires_in="-1",
            scopes=["unknown:*"],
        )
        execute = owner.web_create
    else:
        key_id = key_fixture(db_session)
        command = mutate_command(key_id, owner.WebApiKeyAction(action))
        execute = owner.web_mutate
    if api_actor:
        actor = AuditActor.api_key(str(uuid4()), label="excluded-actor-label")
        command = replace(
            command,
            actor=actor,
            context=replace(command.context, actor=f"api_key:{actor.actor_id}"),
        )
    with trace_transactions(db_session.get_bind().engine) as trace:
        outcome = execute(db_session, command=command)
    assert_protected_tx(trace.result, protected_tables=frozenset({"api_keys"}))
    key = db_session.get(ApiKey, outcome.key_id)
    audit = audit_rows(db_session, outcome.key_id)[0]
    assert (
        audit.action
        == f"auth.api_key.{dict(create='created', revoke='revoked', rotate='rotated')[action]}"
    )
    assert (
        audit.actor_type == command.actor.actor_type
        and audit.actor_id == command.actor.actor_id
    )
    assert audit.actor_label is None
    assert audit.details == dict(
        is_active=key.is_active,
        command_id=str(command.context.command_id),
        correlation_id=str(command.context.correlation_id),
    )
    if action != "revoke":
        assert outcome.secret and key.key_hash == hash_api_key(outcome.secret)
    else:
        assert not key.is_active and key.revoked_at and outcome.secret is None
    evidence = repr(command) + repr(outcome) + str(audit.details) + str(audit.metadata_)
    for excluded in [
        "excluded-label",
        "excluded-actor-label",
        "unknown:*",
        key.key_hash,
        outcome.secret or "excluded-never",
    ]:
        assert excluded not in evidence
    if action == "rotate":
        assert key.label == "excluded-label" and key.scopes == [
            "audit:read",
            "unknown:*",
        ]
        assert key.expires_at < datetime.now(UTC).replace(
            tzinfo=None
        )  # Expired active keys still rotate.


@pytest.mark.parametrize("action", ["create", "revoke", "rotate"])
@pytest.mark.parametrize("failure", ["audit_before", "audit_after", "commit"])
def test_audit_and_commit_failure_roll_back(db_session, monkeypatch, action, failure):
    key_id = key_fixture(db_session) if action != "create" else None
    command = (
        create_command(subscriber_id=None, label="excluded-label", expires_in=None)
        if action == "create"
        else mutate_command(key_id, owner.WebApiKeyAction(action))
    )
    execute = owner.web_create if action == "create" else owner.web_mutate
    stage = owner.stage_audit_event

    def fail_audit(*args, **kwargs):
        if failure == "audit_after":
            stage(*args, **kwargs)
        raise RuntimeError("inert audit failure")

    def fail_commit(db):
        raise RuntimeError("inert commit failure")

    if failure == "commit":
        event.listen(db_session, "before_commit", fail_commit)
    else:
        monkeypatch.setattr(owner, "stage_audit_event", fail_audit)
    try:
        with trace_transactions(db_session.get_bind().engine) as trace:
            with pytest.raises(RuntimeError):
                execute(db_session, command=command)
        assert_protected_tx(
            trace.result, protected_tables=frozenset({"api_keys"}), success=False
        )
        assert not db_session.scalars(select(AuditEvent)).all()
        keys = db_session.scalars(select(ApiKey)).all()
        if action == "create":
            assert not keys
        else:
            assert (
                keys[0].is_active
                and keys[0].key_hash == "inert-fixture-hash"
                and keys[0].revoked_at is None
            )
    finally:
        if failure == "commit":
            event.remove(db_session, "before_commit", fail_commit)


@pytest.mark.parametrize("action", list(owner.WebApiKeyAction))
@pytest.mark.parametrize("scope", ["own", "other", "empty", "absent"])
def test_subscriber_scope_has_no_wildcard(db_session, subscriber, action, scope):
    subscriber_id = subscriber.id
    key_id = key_fixture(db_session, subscriber_id)
    caller = subscriber_id if scope == "own" else uuid4() if scope == "other" else None
    command = mutate_command(key_id, action, caller, invalid=scope == "empty")
    if scope == "empty":
        with pytest.raises(owner.WebApiKeyError, match="Invalid subscriber scope"):
            owner.web_mutate(db_session, command=command)
    else:
        assert (owner.web_mutate(db_session, command=command).key_id is not None) == (
            scope == "own"
        )
    assert len(audit_rows(db_session, key_id)) == (scope == "own")


@pytest.mark.parametrize("action", list(owner.WebApiKeyAction))
def test_staff_bound_null_subscriber_remains_global(db_session, action):
    user = SystemUser(
        first_name="Staff", last_name="Fixture", email=f"{uuid4()}@test.invalid"
    )
    db_session.add(user)
    db_session.flush()
    user_id = user.id
    key = ApiKey(key_hash="inert-staff-hash", system_user_id=user_id)
    db_session.add(key)
    db_session.flush()
    key_id = key.id
    db_session.commit()
    assert (
        owner.web_mutate(
            db_session, command=mutate_command(key_id, action, invalid=True)
        ).key_id
        == key_id
    )
    assert db_session.get(ApiKey, key_id).system_user_id == user_id


@pytest.mark.parametrize("active,revoked", [(False, False), (True, True)])
def test_rotation_rechecks_persisted_standing(db_session, active, revoked):
    key_id = key_fixture(db_session, active=active, revoked=revoked)
    key = db_session.get(ApiKey, key_id)
    db_session.commit()
    # Simulate a stale identity map without sending any UPDATE.
    set_committed_value(key, "is_active", True)
    set_committed_value(key, "revoked_at", None)
    assert (
        owner.web_mutate(
            db_session, command=mutate_command(key_id, owner.WebApiKeyAction.ROTATE)
        ).secret
        is None
    )
    assert not audit_rows(db_session, key_id)


def test_repeat_revoke_and_missing_target(db_session):
    key_id = key_fixture(db_session)
    command = mutate_command(key_id, owner.WebApiKeyAction.REVOKE)
    for _ in range(2):
        assert owner.web_mutate(db_session, command=command).key_id == key_id
    assert len(audit_rows(db_session, key_id)) == 2
    db_session.commit()
    assert (
        owner.web_mutate(
            db_session, command=mutate_command(uuid4(), owner.WebApiKeyAction.REVOKE)
        ).key_id
        is None
    )


@pytest.mark.parametrize("invalid", ["actor", "scope", "target", "caller", "action"])
def test_typed_mutation_refuses_bad_contract(db_session, invalid):
    command = mutate_command(uuid4(), owner.WebApiKeyAction.REVOKE)
    if invalid in {"actor", "scope"}:
        command = replace(
            command, context=replace(command.context, **{invalid: "wrong"})
        )
    else:
        command = replace(
            command,
            **{
                dict(target="key_id", caller="caller_scope", action="action")[
                    invalid
                ]: "wrong"
            },
        )
    with pytest.raises(owner.WebApiKeyError):
        owner.web_mutate(db_session, command=command)
    assert not db_session.in_transaction()


def test_active_request_session_refused(db_session):
    db_session.execute(select(ApiKey))
    with pytest.raises(OwnerCommandError, match="transaction"):
        owner.web_create(
            db_session,
            command=create_command(subscriber_id=None, label="test", expires_in=None),
        )


@pytest.mark.parametrize("action", ["create", "rotate"])
def test_sqlalchemy_error_is_sanitized(db_session, monkeypatch, action):
    key_id = key_fixture(db_session)
    command = (
        create_command(subscriber_id=None, label="excluded-label", expires_in=None)
        if action == "create"
        else mutate_command(key_id, owner.WebApiKeyAction.ROTATE)
    )

    def fail(*args, **kwargs):
        raise OperationalError(
            "secret-bearing-query",
            {"secret": "excluded-secret"},
            Exception("excluded-storage"),
        )

    monkeypatch.setattr(owner, "stage_audit_event", fail)
    with pytest.raises(owner.WebApiKeyError) as error:
        (owner.web_create if action == "create" else owner.web_mutate)(
            db_session, command=command
        )
    assert error.value.code.endswith(".storage_unavailable")
    assert error.value.__cause__ is None and error.value.__suppress_context__
    assert "excluded" not in str(error.value)


def request_fixture():
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/admin/system/api-keys",
            "headers": [],
            "scheme": "http",
            "server": ("testserver", 80),
            "query_string": b"",
        }
    )
    request.state.actor_id = str(uuid4())
    return request


def test_web_adapter_fresh_session_and_composed_one_time_render(
    db_session, monkeypatch
):
    import app.web.admin as admin

    request = request_fixture()
    caller = {"person_id": "", "name": "Staff"}
    monkeypatch.setattr(admin, "get_current_user", lambda request: caller)
    monkeypatch.setattr(admin, "get_sidebar_stats", lambda db: {})
    sessions = []

    @contextmanager
    def fresh():
        sessions.append("open")
        yield db_session
        assert not db_session.in_transaction()
        sessions.append("close")

    monkeypatch.setattr(db_session_adapter, "owner_command_session", fresh)
    response = system.api_key_create(
        request,
        label="Integration",
        expires_in=None,
        scopes="audit:read",
        system_owned="1",
        db=db_session,
    )
    assert response.status_code == 200 and sessions == ["open", "close"]
    secret = response.context["new_key"]
    assert secret.encode() in response.body and "location" not in response.headers
    key_id = response.context["api_keys"][0].id
    db_session.commit()
    response = system.api_key_rotate(request, str(key_id), db_session)
    assert (
        response.context["new_key"] != secret
        and response.context["new_key"].encode() in response.body
    )
    db_session.commit()
    response = system.api_key_revoke(request, str(key_id), db_session)
    assert (
        response.status_code == 303
        and response.headers["location"] == "/admin/system/api-keys?revoked=1"
    )
    assert secret not in response.headers["location"]


def test_staff_own_create_redirect_and_empty_scope_compatibility(
    db_session, monkeypatch, subscriber
):
    import app.web.admin as admin

    monkeypatch.setattr(admin, "get_current_user", lambda request: {"person_id": ""})
    request = request_fixture()
    response = system.api_key_create(
        request,
        label="Staff",
        expires_in=None,
        scopes=None,
        system_owned=None,
        db=db_session,
    )
    assert response.status_code == 303
    key_id = key_fixture(db_session, subscriber.id)

    @contextmanager
    def fresh():
        yield db_session

    monkeypatch.setattr(db_session_adapter, "owner_command_session", fresh)
    with pytest.raises(HTTPException) as error:
        system.api_key_revoke(request, str(key_id), db_session)
    assert error.value.status_code == 500


def test_web_create_bad_days_render_safe_form(db_session, monkeypatch):
    import app.web.admin as admin

    monkeypatch.setattr(admin, "get_current_user", lambda request: {"person_id": ""})
    monkeypatch.setattr(admin, "get_sidebar_stats", lambda db: {})
    response = system.api_key_create(
        request_fixture(),
        label="Staff",
        expires_in="invalid",
        scopes=None,
        system_owned="1",
        db=db_session,
    )
    assert response.status_code == 200
    assert b"Invalid API-key creation input." in response.body


@pytest.mark.parametrize("action", ["create", "revoke", "rotate"])
@pytest.mark.parametrize("denial", ["permission", "actor", "admin_mount"])
def test_real_http_guard_chain_refuses_before_owner_session(
    db_session, monkeypatch, action, denial
):
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient

    import app.web.admin as admin
    from app.db import get_db
    from app.models.rbac import Permission
    from app.models.system_user import UserType
    from app.services import auth_dependencies, erp_staff_access
    from app.web.auth.dependencies import require_admin_web_auth, require_web_auth

    db_session.add(Permission(key="system:settings:write", is_active=True))
    db_session.commit()
    auth = {
        "principal_type": "subscriber" if denial == "admin_mount" else "system_user",
        "subscriber": SystemUser(user_type=UserType.system_user),
        "roles": ["support"],
        "scopes": [] if denial == "permission" else ["system:settings:write"],
    }
    app = FastAPI()
    app.include_router(
        system.router, prefix="/admin", dependencies=[Depends(require_admin_web_auth)]
    )
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_web_auth] = lambda: dict(auth)
    app.dependency_overrides[auth_dependencies.require_user_auth] = lambda: dict(auth)
    monkeypatch.setattr(
        auth_dependencies, "load_permission_keys", lambda auth, db: frozenset()
    )
    monkeypatch.setattr(
        auth_dependencies, "has_permission", lambda auth, db, key: False
    )
    monkeypatch.setattr(
        auth_dependencies, "_enforce_staff_leave_write_guard", lambda **kwargs: None
    )
    monkeypatch.setattr(
        erp_staff_access, "staff_write_restricted", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(admin, "get_current_user", lambda request: {"person_id": ""})

    def forbidden_session():
        pytest.fail("denied request entered command session")

    monkeypatch.setattr(db_session_adapter, "owner_command_session", forbidden_session)
    monkeypatch.setattr(
        owner,
        "web_create",
        lambda *args, **kwargs: pytest.fail("denied request entered owner"),
    )
    monkeypatch.setattr(
        owner,
        "web_mutate",
        lambda *args, **kwargs: pytest.fail("denied request entered owner"),
    )
    path = "/admin/system/api-keys" + (
        "" if action == "create" else f"/{uuid4()}/{action}"
    )
    response = TestClient(app, raise_server_exceptions=False).post(
        path, data={"label": "test", "system_owned": "1"}
    )
    assert response.status_code == 403
    assert response.json()["detail"] == (
        "Forbidden"
        if denial == "permission"
        else "Authorized actor is missing"
        if denial == "actor"
        else "Administrator access is required for this area."
    )
