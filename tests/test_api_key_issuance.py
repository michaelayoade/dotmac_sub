"""Fast SQLite unit evidence; not deployed PostgreSQL schema acceptance."""

from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.api import auth as api
from app.models.audit import AuditEvent
from app.models.auth import ApiKey
from app.schemas.auth import ApiKeyCreate, ApiKeyGenerateRequest
from app.services import api_key_administration as owner
from app.services import auth
from app.services.audit_adapter import AuditActor
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import OwnerCommandError
from tests.api_key_issuance_fixtures import create_command, generate_command
from tests.support.tx_trace import assert_protected_tx, trace_transactions


def issuance(generated: bool):
    if generated:
        return owner.generate, generate_command(
            ApiKeyGenerateRequest(label="excluded-label")
        )
    return owner.create, create_command(
        ApiKeyCreate(key_hash="excluded-preimage", label="excluded-label")
    )


@pytest.mark.parametrize("generated", [False, True])
@pytest.mark.parametrize("api_key_actor", [False, True])
def test_issuance_state_and_canonical_audit_share_root(
    db_session: Session, generated: bool, api_key_actor: bool
) -> None:
    execute, command = issuance(generated)
    if api_key_actor:
        actor = AuditActor.api_key(str(uuid4()), label="excluded-actor-label")
        command = replace(
            command, actor=actor, context=replace(command.context, actor=actor.actor_id)
        )
    with trace_transactions(db_session.get_bind().engine) as trace:
        outcome = execute(db_session, command=command)
    assert_protected_tx(trace.result, protected_tables=frozenset({"api_keys"}))
    key = outcome.api_key if generated else outcome
    raw_key = outcome.key if generated else command.supplied_secret
    row = db_session.get(ApiKey, key.id)
    assert row.key_hash == auth.hash_api_key(raw_key)
    assert row.scopes == []  # Empty scopes remain deny-all at runtime.
    assert row.is_active and row.expires_at is None
    audit = db_session.scalars(
        select(AuditEvent).where(AuditEvent.entity_id == str(key.id))
    ).one()
    assert audit.action == (
        "auth.api_key.generated" if generated else "auth.api_key.created"
    )
    assert audit.actor_id == command.actor.actor_id
    assert audit.actor_type == command.actor.actor_type
    assert audit.actor_label is None
    assert audit.details == {
        "command_id": str(command.context.command_id),
        "correlation_id": str(command.context.correlation_id),
        "is_active": True,
    }
    evidence = str(audit.details) + str(audit.metadata_) + repr(command) + repr(outcome)
    for excluded in (raw_key, row.key_hash, "excluded-label", "excluded-actor-label"):
        assert excluded not in evidence
    with pytest.raises(FrozenInstanceError):
        key.is_active = False


def test_create_preserves_all_legacy_fields(db_session: Session) -> None:
    past = datetime.now(UTC) - timedelta(days=2)
    expires = past + timedelta(days=1)
    command = create_command(
        ApiKeyCreate(
            key_hash="supplied-preimage",
            scopes=["*", "customer:read", "custom"],
            is_active=False,
            last_used_at=past,
            revoked_at=past,
            expires_at=expires,
        )
    )
    issued = owner.create(db_session, command=command)
    assert issued.scopes == ("*", "customer:read", "custom")
    assert issued.last_used_at == past and issued.revoked_at == past
    assert issued.expires_at == expires and issued.is_active is False
    assert issued.key_hash == auth.hash_api_key("supplied-preimage")


@pytest.mark.parametrize("generated", [False, True])
@pytest.mark.parametrize("failure", ["audit_before", "audit_after", "commit"])
def test_failure_rolls_back_key_and_audit(
    monkeypatch: pytest.MonkeyPatch, db_session: Session, generated: bool, failure: str
) -> None:
    execute, command = issuance(generated)
    stage = owner.stage_audit_event

    def fail_audit(*args, **kwargs):
        if failure == "audit_after":
            stage(*args, **kwargs)
        raise RuntimeError("inert staged failure")

    def fail_commit(session):
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
        assert not db_session.scalars(select(ApiKey)).all()
        assert not db_session.scalars(select(AuditEvent)).all()
    finally:
        if failure == "commit":
            event.remove(db_session, "before_commit", fail_commit)


@pytest.mark.parametrize("generated", [False, True])
@pytest.mark.parametrize(
    "invalid",
    [
        "command",
        "context",
        "actor",
        "system",
        "scope",
        "mismatched_actor",
        "subscriber",
        "scopes",
        "expires",
    ],
)
def test_malformed_and_untrusted_commands_refused(
    db_session: Session, generated: bool, invalid: str
) -> None:
    execute, command = issuance(generated)
    if invalid == "command":
        command = object()
    elif invalid in {"context", "actor"}:
        command = replace(command, **{invalid: object()})
    elif invalid == "system":
        command = replace(command, actor=AuditActor.system("inert"))
    elif invalid == "scope":
        command = replace(command, context=replace(command.context, scope="wrong"))
    elif invalid == "mismatched_actor":
        command = replace(command, context=replace(command.context, actor="wrong"))
    elif invalid == "subscriber":
        command = replace(command, subscriber_id="not-a-uuid")
    elif invalid == "scopes":
        command = replace(command, scopes=["*"])
    else:
        command = replace(command, expires_at="not-a-datetime")
    with pytest.raises(owner.ApiKeyIssuanceError):
        execute(db_session, command=command)
    assert not db_session.in_transaction()


@pytest.mark.parametrize("generated", [False, True])
def test_missing_subscriber_and_active_caller_transaction(
    db_session: Session, generated: bool
) -> None:
    execute, command = issuance(generated)
    with pytest.raises(owner.ApiKeyIssuanceError) as missing:
        execute(db_session, command=replace(command, subscriber_id=uuid4()))
    assert missing.value.code.endswith(".subscriber_not_found")
    db_session.execute(select(ApiKey))
    with pytest.raises(OwnerCommandError) as error:
        execute(db_session, command=command)
    assert error.value.code.endswith(".active_caller_transaction")


def test_duplicate_preimage_unique_backstop_no_extra_audit(db_session: Session) -> None:
    command = create_command(ApiKeyCreate(key_hash="same-inert-preimage"))
    owner.create(db_session, command=command)
    with pytest.raises(owner.ApiKeyIssuanceError) as error:
        owner.create(db_session, command=command)
    assert error.value.code.endswith(".storage_unavailable")
    assert "same-inert-preimage" not in str(error.value)
    assert "hmac256" not in str(error.value)
    assert error.value.__cause__ is None and error.value.__suppress_context__
    assert len(db_session.scalars(select(ApiKey)).all()) == 1
    assert len(db_session.scalars(select(AuditEvent)).all()) == 1


@pytest.mark.parametrize("generated", [False, True])
@pytest.mark.parametrize("api_key_actor", [False, True])
def test_routes_fresh_session_snapshot_and_masking(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
    generated: bool,
    api_key_actor: bool,
) -> None:
    seen = []

    @contextmanager
    def fresh():
        seen.append("open")
        yield db_session
        assert not db_session.in_transaction()
        seen.append("close")

    def limit(db, *, request):
        assert db is request_db
        seen.append("limit")

    request_db = object()
    actor_id = str(uuid4())
    auth_context = {
        "principal_id": str(uuid4()),
        "api_key_id": actor_id if api_key_actor else None,
    }
    monkeypatch.setattr(db_session_adapter, "owner_command_session", fresh)
    monkeypatch.setattr(auth.api_keys, "enforce_generation_rate_limit", limit)
    if generated:
        response = api.generate_api_key(
            ApiKeyGenerateRequest(scopes=["customer:read"]),
            Request({"type": "http"}),
            request_db,
            auth_context,
        )
        key = response.api_key
        raw = response.key
        assert seen == ["limit", "open", "close"]
    else:
        response = api.create_api_key(
            ApiKeyCreate(key_hash="route-inert-preimage"), auth_context
        )
        key, raw = response, "route-inert-preimage"
        assert seen == ["open", "close"]
    assert key.key_hash == auth.hash_api_key(raw)
    assert key.model_dump(mode="json")["key_hash"] != key.key_hash
    audit = db_session.scalars(select(AuditEvent)).one()
    assert audit.actor_id == (
        actor_id if api_key_actor else auth_context["principal_id"]
    )


@pytest.mark.parametrize("generated", [False, True])
def test_routes_missing_subscriber_404(
    monkeypatch: pytest.MonkeyPatch, db_session: Session, generated: bool
) -> None:
    @contextmanager
    def fresh():
        yield db_session

    monkeypatch.setattr(db_session_adapter, "owner_command_session", fresh)
    monkeypatch.setattr(
        auth.api_keys, "enforce_generation_rate_limit", lambda *args, **kwargs: None
    )
    with pytest.raises(HTTPException) as error:
        if generated:
            api.generate_api_key(
                ApiKeyGenerateRequest(subscriber_id=uuid4()),
                Request({"type": "http"}),
                object(),
                {"principal_id": str(uuid4())},
            )
        else:
            api.create_api_key(
                ApiKeyCreate(key_hash="inert", subscriber_id=uuid4()),
                {"principal_id": str(uuid4())},
            )
    assert error.value.status_code == 404


@pytest.mark.parametrize("status", [429, 503])
def test_rate_denial_precedes_session_and_secret(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    def deny(*args, **kwargs):
        raise HTTPException(status_code=status, detail="inert denial")

    def forbidden(*args, **kwargs):
        pytest.fail("rate denial reached key generation")

    monkeypatch.setattr(auth.api_keys, "enforce_generation_rate_limit", deny)
    monkeypatch.setattr(db_session_adapter, "owner_command_session", forbidden)
    monkeypatch.setattr(owner.secrets, "token_urlsafe", forbidden)
    with pytest.raises(HTTPException) as error:
        api.generate_api_key(
            ApiKeyGenerateRequest(),
            Request({"type": "http"}),
            object(),
            {"principal_id": str(uuid4())},
        )
    assert error.value.status_code == status


@pytest.mark.parametrize("generated", [False, True])
def test_storage_exception_is_sanitized(
    monkeypatch: pytest.MonkeyPatch, db_session: Session, generated: bool
) -> None:
    from sqlalchemy.exc import OperationalError

    execute, command = issuance(generated)

    def fail_flush(*args, **kwargs):
        raise OperationalError(
            "INSERT inert",
            {"key_hash": "excluded-hash", "label": "excluded-label"},
            RuntimeError("inert database failure"),
        )

    monkeypatch.setattr(db_session, "flush", fail_flush)
    with pytest.raises(owner.ApiKeyIssuanceError) as error:
        execute(db_session, command=command)
    assert error.value.code.endswith(".storage_unavailable")
    assert not db_session.in_transaction()
    assert "excluded" not in str(error.value) + repr(error.value)
    assert error.value.__suppress_context__ and error.value.__cause__ is None


@pytest.mark.parametrize("generated", [False, True])
def test_route_maps_sanitized_storage_failure_to_500(
    monkeypatch: pytest.MonkeyPatch, db_session: Session, generated: bool
) -> None:
    @contextmanager
    def fresh():
        yield db_session

    def fail(*args, **kwargs):
        raise owner.ApiKeyIssuanceError(
            code=f"{owner.OWNER}.storage_unavailable",
            message="API-key issuance unavailable.",
        )

    monkeypatch.setattr(db_session_adapter, "owner_command_session", fresh)
    monkeypatch.setattr(
        auth.api_keys, "enforce_generation_rate_limit", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(owner, "generate" if generated else "create", fail)
    with pytest.raises(HTTPException) as error:
        if generated:
            api.generate_api_key(
                ApiKeyGenerateRequest(),
                Request({"type": "http"}),
                object(),
                {"principal_id": str(uuid4())},
            )
        else:
            api.create_api_key(
                ApiKeyCreate(key_hash="inert"), {"principal_id": str(uuid4())}
            )
    assert error.value.status_code == 500
    assert error.value.detail == "API-key issuance unavailable."
