"""SQLite fast unit evidence for the audit-only JSON PATCH ownership cutover."""

from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import String, cast, event, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from app.api.auth import update_api_key
from app.models.audit import AuditEvent
from app.models.auth import ApiKey
from app.models.system_user import SystemUser
from app.schemas.auth import ApiKeyUpdate
from app.services import api_key_administration as owner
from app.services.audit_adapter import AuditActor
from app.services.auth import hash_api_key
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import OwnerCommandError
from tests.api_key_update_fixtures import update_command
from tests.support.tx_trace import assert_protected_tx, trace_transactions
from tests.test_api_key_administration import key_fixture


def audits(db: Session, key_id: UUID) -> list[AuditEvent]:
    return list(
        db.scalars(select(AuditEvent).where(AuditEvent.entity_id == str(key_id)))
    )


@pytest.mark.parametrize("api_key_actor", [False, True])
def test_update_state_audit_and_safe_evidence(
    db_session: Session, api_key_actor: bool
) -> None:
    key_id = key_fixture(db_session)
    command = update_command(
        key_id,
        ApiKeyUpdate(
            label="excluded-new-label",
            key_hash="excluded-preimage",
            scopes=["*", "unknown:*", "unknown:*"],
            is_active=False,
        ),
    )
    if api_key_actor:
        actor = AuditActor.api_key(str(uuid4()), label="excluded-actor-label")
        command = replace(
            command, actor=actor, context=replace(command.context, actor=actor.actor_id)
        )
    with trace_transactions(db_session.get_bind().engine) as trace:
        outcome = owner.update(db_session, command=command)
    assert_protected_tx(trace.result, protected_tables=frozenset({"api_keys"}))
    assert outcome.key_hash == hash_api_key("excluded-preimage")
    assert outcome.label == "excluded-new-label"
    assert outcome.scopes == ("*", "unknown:*", "unknown:*")
    assert not outcome.is_active
    audit = audits(db_session, key_id)[0]
    assert audit.action == "auth.api_key.updated"
    assert (
        audit.actor_id == command.actor.actor_id
        and audit.actor_type == command.actor.actor_type
    )
    assert audit.actor_label is None
    assert audit.details == {
        "updated_fields": ["is_active", "key_hash", "label", "scopes"],
        "hash_changed": True,
        "command_id": str(command.context.command_id),
        "correlation_id": str(command.context.correlation_id),
    }
    evidence = repr(command) + repr(outcome) + str(audit.details) + str(audit.metadata_)
    for excluded in (
        "excluded-preimage",
        "excluded-new-label",
        "excluded-actor-label",
        outcome.key_hash,
        "unknown:*",
    ):
        assert excluded not in evidence


def test_omitted_fields_remain_and_noop_is_audited(db_session: Session) -> None:
    key_id = key_fixture(db_session)
    for _ in range(2):
        outcome = owner.update(
            db_session, command=update_command(key_id, ApiKeyUpdate())
        )
        assert outcome.label == "excluded-label"
        assert outcome.key_hash == "inert-test-hash" and outcome.is_active is True
        assert outcome.scopes == ()
    audit = audits(db_session, key_id)
    assert len(audit) == 2
    assert all(
        row.details["updated_fields"] == [] and row.details["hash_changed"] is False
        for row in audit
    )


def test_hash_same_preimage_does_not_claim_changed(db_session: Session) -> None:
    key_id = key_fixture(db_session)
    command = update_command(key_id, ApiKeyUpdate(key_hash="same-preimage"))
    owner.update(db_session, command=command)
    owner.update(db_session, command=command)
    assert [audit.details["hash_changed"] for audit in audits(db_session, key_id)] == [
        True,
        False,
    ]


def test_explicit_nullable_fields_clear_and_reactivation_preserves_expiry(
    db_session: Session, person
) -> None:
    past = datetime.now(UTC) - timedelta(days=1)
    key = ApiKey(
        key_hash="initial-inert",
        subscriber_id=person.id,
        label="initial",
        is_active=False,
        revoked_at=past,
        last_used_at=past,
        expires_at=past,
    )
    db_session.add(key)
    db_session.flush()
    key_id = key.id
    db_session.commit()
    outcome = owner.update(
        db_session,
        command=update_command(
            key_id,
            ApiKeyUpdate(
                subscriber_id=None,
                label=None,
                revoked_at=None,
                last_used_at=None,
                is_active=True,
            ),
        ),
    )
    assert outcome.subscriber_id is None and outcome.label is None
    assert outcome.revoked_at is None and outcome.last_used_at is None
    assert outcome.is_active is True
    assert outcome.expires_at.replace(tzinfo=UTC) == past
    outcome = owner.update(
        db_session,
        command=update_command(
            key_id, ApiKeyUpdate(expires_at=None, last_used_at=past)
        ),
    )
    assert outcome.expires_at is None and outcome.last_used_at == past


def test_owner_update_retains_system_user_and_has_no_new_xor_policy(
    db_session: Session, person
) -> None:
    user = SystemUser(
        first_name="Staff", last_name="Fixture", email=f"{uuid4()}@test.invalid"
    )
    db_session.add(user)
    db_session.flush()
    user_id, subscriber_id = user.id, person.id
    key = ApiKey(key_hash="owner-collision-inert", system_user_id=user_id)
    db_session.add(key)
    db_session.flush()
    key_id = key.id
    db_session.commit()
    outcome = owner.update(
        db_session,
        command=update_command(key_id, ApiKeyUpdate(subscriber_id=subscriber_id)),
    )
    assert outcome.subscriber_id == subscriber_id and outcome.system_user_id == user_id
    outcome = owner.update(
        db_session, command=update_command(key_id, ApiKeyUpdate(subscriber_id=None))
    )
    assert outcome.subscriber_id is None and outcome.system_user_id == user_id


def test_scopes_none_persists_json_null(db_session: Session) -> None:
    key_id = key_fixture(db_session)
    outcome = owner.update(
        db_session, command=update_command(key_id, ApiKeyUpdate(scopes=None))
    )
    assert outcome.scopes is None
    sql_null, encoded = db_session.execute(
        select(ApiKey.scopes.is_(None), cast(ApiKey.scopes, String)).where(
            ApiKey.id == key_id
        )
    ).one()
    assert sql_null is False and encoded == "null"
    assert audits(db_session, key_id)[0].details["updated_fields"] == ["scopes"]


@pytest.mark.parametrize("field", ["key_hash", "is_active"])
def test_explicit_nonnullable_null_storage_failure_rolls_back(
    db_session: Session, field: str
) -> None:
    key_id = key_fixture(db_session)
    with pytest.raises(owner.ApiKeyUpdateError) as error:
        owner.update(
            db_session, command=update_command(key_id, ApiKeyUpdate(**{field: None}))
        )
    assert error.value.code.endswith(".storage_unavailable")
    assert error.value.__suppress_context__ and error.value.__cause__ is None
    row = db_session.get(ApiKey, key_id)
    assert row.key_hash == "inert-test-hash" and row.is_active is True
    assert not audits(db_session, key_id)


@pytest.mark.parametrize("failure", ["audit_before", "audit_after", "commit"])
def test_audit_or_commit_failure_rolls_back(
    monkeypatch: pytest.MonkeyPatch, db_session: Session, failure: str
) -> None:
    key_id = key_fixture(db_session)
    stage = owner.stage_audit_event

    def fail_audit(*args, **kwargs):
        if failure == "audit_after":
            stage(*args, **kwargs)
        raise RuntimeError("inert audit failure")

    def fail_commit(session):
        raise RuntimeError("inert commit failure")

    if failure == "commit":
        event.listen(db_session, "before_commit", fail_commit)
    else:
        monkeypatch.setattr(owner, "stage_audit_event", fail_audit)
    try:
        with trace_transactions(db_session.get_bind().engine) as trace:
            with pytest.raises(RuntimeError):
                owner.update(
                    db_session,
                    command=update_command(
                        key_id, ApiKeyUpdate(key_hash="excluded-preimage")
                    ),
                )
        assert_protected_tx(
            trace.result, protected_tables=frozenset({"api_keys"}), success=False
        )
        assert db_session.get(ApiKey, key_id).key_hash == "inert-test-hash"
        assert not audits(db_session, key_id)
    finally:
        if failure == "commit":
            event.remove(db_session, "before_commit", fail_commit)


def test_refreshed_lock_ignores_stale_identity_map(db_session: Session) -> None:
    key_id = key_fixture(db_session)
    row = db_session.get(ApiKey, key_id)
    db_session.expire_on_commit = False
    db_session.commit()
    set_committed_value(row, "key_hash", hash_api_key("next-preimage"))
    set_committed_value(row, "label", "stale-label")
    outcome = owner.update(
        db_session,
        command=update_command(key_id, ApiKeyUpdate(key_hash="next-preimage")),
    )
    assert outcome.label == "excluded-label"
    assert audits(db_session, key_id)[0].details["hash_changed"] is True


@pytest.mark.parametrize(
    "invalid",
    [
        "command",
        "context",
        "actor",
        "provenance",
        "system",
        "scope",
        "mismatch",
        "target",
        "fields",
        "unknown",
        "subscriber",
        "label",
        "secret",
        "scopes",
        "active",
        "date",
    ],
)
def test_malformed_or_untrusted_commands_refused(
    db_session: Session, invalid: str
) -> None:
    command = update_command(uuid4(), ApiKeyUpdate())
    if invalid == "command":
        command = object()
    elif invalid in {"context", "actor"}:
        command = replace(command, **{invalid: object()})
    elif invalid == "provenance":
        object.__setattr__(command, "provenance", "authenticated_administrator")
    elif invalid == "system":
        command = replace(command, actor=AuditActor.system("inert"))
    elif invalid in {"scope", "mismatch"}:
        command = replace(
            command,
            context=replace(
                command.context, **{"scope" if invalid == "scope" else "actor": "wrong"}
            ),
        )
    else:
        values = {
            "target": {"key_id": "wrong"},
            "fields": {"fields": set()},
            "unknown": {"fields": frozenset({"system_user_id"})},
            "subscriber": {"subscriber_id": "wrong"},
            "label": {"label": 42},
            "secret": {"supplied_secret": ""},
            "scopes": {"scopes": ["*"]},
            "active": {"is_active": 1},
            "date": {"expires_at": "wrong"},
        }
        command = replace(command, **values[invalid])
    with pytest.raises(owner.ApiKeyUpdateError):
        owner.update(db_session, command=command)
    assert not db_session.in_transaction()


def test_missing_target_subscriber_and_active_caller_refused(
    db_session: Session,
) -> None:
    with pytest.raises(owner.ApiKeyUpdateError) as error:
        owner.update(db_session, command=update_command(uuid4(), ApiKeyUpdate()))
    assert error.value.code.endswith(".not_found")
    key_id = key_fixture(db_session)
    with pytest.raises(owner.ApiKeyUpdateError) as error:
        owner.update(
            db_session,
            command=update_command(key_id, ApiKeyUpdate(subscriber_id=uuid4())),
        )
    assert error.value.code.endswith(".subscriber_not_found")
    db_session.get(ApiKey, key_id)
    with pytest.raises(OwnerCommandError) as error:
        owner.update(db_session, command=update_command(key_id, ApiKeyUpdate()))
    assert error.value.code.endswith(".active_caller_transaction")


@pytest.mark.parametrize("api_key_actor", [False, True])
def test_adapter_fresh_session_fields_actor_and_hash_mask(
    monkeypatch: pytest.MonkeyPatch, db_session: Session, api_key_actor: bool
) -> None:
    key_id = key_fixture(db_session)
    seen = []

    @contextmanager
    def fresh():
        seen.append("open")
        yield db_session
        assert not db_session.in_transaction()
        seen.append("close")

    monkeypatch.setattr(db_session_adapter, "owner_command_session", fresh)
    actor_id = str(uuid4())
    auth = {"principal_id": actor_id, "api_key_id": actor_id if api_key_actor else None}
    response = update_api_key(
        str(key_id), ApiKeyUpdate(key_hash="adapter-preimage", label=None), auth
    )
    assert seen == ["open", "close"]
    assert response.label is None and response.key_hash == hash_api_key(
        "adapter-preimage"
    )
    assert response.model_dump(mode="json")["key_hash"] != response.key_hash
    audit = audits(db_session, key_id)[0]
    assert audit.actor_id == actor_id and audit.actor_type.value == (
        "api_key" if api_key_actor else "user"
    )
    assert audit.details["updated_fields"] == ["key_hash", "label"]


@pytest.mark.parametrize("case", ["path", "target", "subscriber", "key_hash", "scopes"])
def test_adapter_preserves_failure_status_classes(
    monkeypatch: pytest.MonkeyPatch, db_session: Session, case: str
) -> None:
    key_id = key_fixture(db_session)

    @contextmanager
    def fresh():
        yield db_session

    monkeypatch.setattr(db_session_adapter, "owner_command_session", fresh)
    target = (
        "malformed-path"
        if case == "path"
        else str(uuid4())
        if case == "target"
        else str(key_id)
    )
    payload = (
        ApiKeyUpdate(subscriber_id=uuid4())
        if case == "subscriber"
        else ApiKeyUpdate(**{case: None})
        if case in {"key_hash", "scopes"}
        else ApiKeyUpdate()
    )
    with pytest.raises(HTTPException) as error:
        update_api_key(target, payload, {"principal_id": str(uuid4())})
    assert error.value.status_code == (404 if case in {"target", "subscriber"} else 500)
    assert "malformed-path" not in str(error.value)
    if case == "scopes":
        assert db_session.get(ApiKey, key_id).scopes is None
        assert (
            len(audits(db_session, key_id)) == 1
        )  # Committed, then read-schema rejection.
    else:
        assert not audits(db_session, key_id)


@pytest.mark.parametrize("sqlstate", ["55P03", "08006"])
def test_storage_errors_sanitized_and_lock_timeout_mapped(
    monkeypatch: pytest.MonkeyPatch, db_session: Session, sqlstate: str
) -> None:
    key_id = key_fixture(db_session)

    class Failure(Exception):
        pass

    cause = Failure("inert")
    cause.sqlstate = sqlstate

    def fail(*args, **kwargs):
        raise OperationalError(
            "inert", {"key_hash": "excluded-hash", "label": "excluded-label"}, cause
        )

    monkeypatch.setattr(db_session, "flush", fail)
    with pytest.raises(owner.ApiKeyUpdateError) as error:
        owner.update(
            db_session,
            command=update_command(key_id, ApiKeyUpdate(label="excluded-label")),
        )
    assert error.value.code.endswith(
        ".busy" if sqlstate == "55P03" else ".storage_unavailable"
    )
    assert "excluded" not in str(error.value) + repr(error.value)
    assert error.value.__cause__ is None and error.value.__suppress_context__

    @contextmanager
    def fresh():
        yield db_session

    monkeypatch.setattr(db_session_adapter, "owner_command_session", fresh)
    with pytest.raises(HTTPException) as response:
        update_api_key(
            str(key_id),
            ApiKeyUpdate(label="excluded-label"),
            {"principal_id": str(uuid4())},
        )
    assert response.value.status_code == (503 if sqlstate == "55P03" else 500)
    assert response.value.headers == (
        {"Retry-After": "5"} if sqlstate == "55P03" else None
    )


def test_subscriber_transfer_preserves_other_fields(
    db_session: Session, person
) -> None:
    from app.models.subscriber import Subscriber

    second = Subscriber(
        first_name="Second", last_name="Owner", email=f"{uuid4()}@test.invalid"
    )
    db_session.add(second)
    db_session.flush()
    second_id = second.id
    key = ApiKey(
        key_hash="inert-transfer-hash",
        subscriber_id=person.id,
        scopes=["unknown:scope"],
        is_active=False,
    )
    db_session.add(key)
    db_session.flush()
    key_id = key.id
    db_session.commit()
    outcome = owner.update(
        db_session,
        command=update_command(key_id, ApiKeyUpdate(subscriber_id=second_id)),
    )
    assert outcome.subscriber_id == second_id
    assert not outcome.is_active and outcome.scopes == ("unknown:scope",)
    audit = audits(db_session, key_id)[0]
    assert audit.details["updated_fields"] == ["subscriber_id"]
    assert str(second_id) not in str(audit.details) + str(audit.metadata_)


def test_reactivation_and_expiry_keep_existing_runtime_authority(
    db_session: Session,
) -> None:
    from app.services.auth_dependencies import require_audit_auth

    raw = "inert-reactivation-preimage"
    key = ApiKey(
        key_hash=hash_api_key(raw),
        scopes=[],
        is_active=False,
        revoked_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) - timedelta(days=1),
    )
    db_session.add(key)
    db_session.flush()
    key_id = key.id
    db_session.commit()
    owner.update(
        db_session,
        command=update_command(
            key_id, ApiKeyUpdate(is_active=True, revoked_at=None, scopes=["audit:*"])
        ),
    )
    with pytest.raises(HTTPException) as refusal:
        require_audit_auth(
            authorization=None, x_session_token=None, x_api_key=raw, db=db_session
        )
    assert refusal.value.status_code == 401  # Reactivation does not bypass expiry.
    db_session.commit()
    owner.update(
        db_session, command=update_command(key_id, ApiKeyUpdate(expires_at=None))
    )
    accepted = require_audit_auth(
        authorization=None, x_session_token=None, x_api_key=raw, db=db_session
    )
    assert accepted["actor_type"] == "api_key" and accepted["actor_id"] == str(key_id)
