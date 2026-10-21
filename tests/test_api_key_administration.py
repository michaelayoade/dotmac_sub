"""Non-authoritative unit evidence for the bounded API-key revocation owner."""

from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AuditEvent
from app.models.auth import ApiKey
from app.services import api_key_administration as owner
from app.services.audit_adapter import AuditActor
from app.services.owner_commands import CommandContext, OwnerCommandError
from tests.support.tx_trace import assert_protected_tx, trace_transactions


def command(key_id: UUID, actor: AuditActor | None = None) -> owner.RevokeApiKeyCommand:
    actor = actor or AuditActor.user(str(uuid4()))
    assert actor.actor_id is not None
    return owner.RevokeApiKeyCommand(
        context=CommandContext.system(
            actor=actor.actor_id, scope=owner.SCOPE, reason="test"
        ),
        key_id=key_id,
        actor=actor,
    )


def key_fixture(db_session: Session) -> UUID:
    key = ApiKey(label="excluded-label", key_hash="inert-test-hash", is_active=True)
    db_session.add(key)
    db_session.flush()
    key_id = key.id
    db_session.commit()
    return key_id


@pytest.mark.parametrize("actor_factory", [AuditActor.user, AuditActor.api_key])
def test_revoke_trace_and_repeat(db_session: Session, actor_factory) -> None:
    key_id = key_fixture(db_session)
    request = command(key_id, actor_factory(str(uuid4()), label="excluded-actor-label"))
    with trace_transactions(db_session.get_bind().engine) as trace:
        outcome = owner.revoke(db_session, command=request)
    assert_protected_tx(trace.result, protected_tables=frozenset({"api_keys"}))
    assert outcome.key_id == key_id
    row = db_session.get(ApiKey, key_id)
    assert not row.is_active
    audit = db_session.scalars(
        select(AuditEvent).where(AuditEvent.entity_id == str(key_id))
    ).one()
    assert audit.actor_type == request.actor.actor_type
    assert audit.actor_id == request.actor.actor_id
    assert audit.actor_label is None
    assert audit.action == "auth.api_key.revoked"
    assert set(audit.details) == {
        "command_id",
        "correlation_id",
        "old_is_active",
        "old_revoked_at",
        "is_active",
        "revoked_at",
    }
    assert set(audit.metadata_) == {"command_id", "correlation_id"}
    assert "excluded" not in str(audit.details) + str(audit.metadata_)
    db_session.commit()
    again = owner.revoke(db_session, command=command(key_id))
    assert again.revoked_at >= outcome.revoked_at
    assert (
        len(
            db_session.scalars(
                select(AuditEvent).where(AuditEvent.entity_id == str(key_id))
            ).all()
        )
        == 2
    )


def test_audit_failure_rolls_back_without_outcome(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    key_id = key_fixture(db_session)

    def fail(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(owner, "stage_audit_event", fail)
    with trace_transactions(db_session.get_bind().engine) as trace:
        with pytest.raises(RuntimeError, match="audit unavailable"):
            owner.revoke(db_session, command=command(key_id))
    assert_protected_tx(
        trace.result, protected_tables=frozenset({"api_keys"}), success=False
    )
    row = db_session.get(ApiKey, key_id)
    assert row.is_active and row.revoked_at is None
    assert not db_session.scalars(
        select(AuditEvent).where(AuditEvent.entity_id == str(key_id))
    ).all()


def test_missing_key(db_session: Session) -> None:
    with pytest.raises(owner.ApiKeyRevocationError, match="API key not found"):
        owner.revoke(db_session, command=command(uuid4()))


@pytest.mark.parametrize("invalid", ["system", "scope", "actor", "uuid"])
def test_invalid_actor_or_context(db_session: Session, invalid: str) -> None:
    request = command(uuid4())
    if invalid == "system":
        request = command(uuid4(), AuditActor.system("test"))
    elif invalid == "uuid":
        request = replace(request, key_id="untyped")
    else:
        request = replace(
            request, context=replace(request.context, **{invalid: "wrong"})
        )
    with pytest.raises(owner.ApiKeyRevocationError):
        owner.revoke(db_session, command=request)


def test_active_caller_transaction_rejected(db_session: Session) -> None:
    key_id = key_fixture(db_session)
    db_session.get(ApiKey, key_id)
    with pytest.raises(OwnerCommandError) as error:
        owner.revoke(db_session, command=command(key_id))
    assert error.value.code.endswith(".active_caller_transaction")
    assert db_session.get(ApiKey, key_id).is_active


def test_locked_read_refreshes_stale_identity_map(db_session: Session) -> None:
    from sqlalchemy.orm.attributes import set_committed_value

    key_id = key_fixture(db_session)
    row = db_session.get(ApiKey, key_id)
    db_session.expire_on_commit = False
    db_session.commit()
    set_committed_value(row, "is_active", False)
    assert not row.is_active  # Deliberately stale committed ORM observation.
    owner.revoke(db_session, command=command(key_id))
    audit = db_session.scalars(
        select(AuditEvent).where(AuditEvent.entity_id == str(key_id))
    ).one()
    assert audit.details["old_is_active"] is True


@pytest.mark.parametrize("api_key_actor", [False, True])
def test_route_trusted_actor_and_fresh_session(
    monkeypatch: pytest.MonkeyPatch, api_key_actor: bool
) -> None:
    from contextlib import contextmanager

    from app.api.auth import delete_api_key
    from app.services.db_session_adapter import db_session_adapter

    fresh = object()
    seen = []

    @contextmanager
    def command_session():
        seen.append("open")
        try:
            yield fresh
        finally:
            seen.append("close")

    def revoke(db, *, command):
        assert db is fresh
        assert command.actor.actor_id == (key_actor if api_key_actor else principal)
        assert command.actor.actor_type.value == (
            "api_key" if api_key_actor else "user"
        )
        seen.append(command.key_id)

    principal, key_actor, target = str(uuid4()), str(uuid4()), uuid4()
    monkeypatch.setattr(db_session_adapter, "owner_command_session", command_session)
    monkeypatch.setattr(owner, "revoke", revoke)
    assert (
        delete_api_key(
            target,
            {
                "principal_id": principal,
                "api_key_id": key_actor if api_key_actor else None,
            },
        )
        is None
    )
    assert seen == ["open", target, "close"]


def test_route_missing_target_is_404(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    from contextlib import contextmanager

    from fastapi import HTTPException

    from app.api.auth import delete_api_key
    from app.services.db_session_adapter import db_session_adapter

    @contextmanager
    def fresh():
        yield db_session

    monkeypatch.setattr(db_session_adapter, "owner_command_session", fresh)
    with pytest.raises(HTTPException) as error:
        delete_api_key(uuid4(), {"principal_id": str(uuid4())})
    assert error.value.status_code == 404


@pytest.mark.parametrize("field", ["command", "context", "actor"])
def test_malformed_typed_boundary_refused(db_session: Session, field: str) -> None:
    request = command(uuid4())
    malformed = (
        object() if field == "command" else replace(request, **{field: object()})
    )
    with pytest.raises(owner.ApiKeyRevocationError):
        owner.revoke(db_session, command=malformed)
    assert not db_session.in_transaction()


def test_empty_context_and_blank_actor_refused(db_session: Session) -> None:
    with pytest.raises(ValueError):
        AuditActor.user(" ")
    with pytest.raises(ValueError):
        AuditActor.api_key(" ")
    request = command(uuid4())
    with pytest.raises(OwnerCommandError) as error:
        owner.revoke(
            db_session,
            command=replace(request, context=replace(request.context, actor=" ")),
        )
    assert error.value.code.endswith(".invalid_command_context")


@pytest.mark.parametrize("sqlstate", ["55P03", "08006"])
def test_route_lock_timeout_and_unexpected_db_error(
    monkeypatch: pytest.MonkeyPatch, db_session: Session, sqlstate: str
) -> None:
    from contextlib import contextmanager

    from fastapi import HTTPException
    from sqlalchemy.exc import OperationalError

    from app.api.auth import delete_api_key
    from app.services.db_session_adapter import db_session_adapter

    class DatabaseFailure(Exception):
        pass

    failure = DatabaseFailure()
    failure.sqlstate = sqlstate
    database_error = OperationalError("inert", {}, failure)

    @contextmanager
    def fresh():
        yield db_session

    def fail(db, *, command):
        raise database_error

    monkeypatch.setattr(db_session_adapter, "owner_command_session", fresh)
    monkeypatch.setattr(owner, "revoke", fail)
    if sqlstate == "55P03":
        with pytest.raises(HTTPException) as error:
            delete_api_key(uuid4(), {"principal_id": str(uuid4())})
        assert error.value.status_code == 503
        assert error.value.headers == {"Retry-After": "5"}
    else:
        with pytest.raises(OperationalError) as error:
            delete_api_key(uuid4(), {"principal_id": str(uuid4())})
        assert error.value is database_error
