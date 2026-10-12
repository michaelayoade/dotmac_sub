"""Fast inert-key unit evidence; migrated PostgreSQL owns concurrency acceptance."""

from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event, select
from sqlalchemy import update as sql_update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.sql.dml import Update

from app.models.audit import AuditEvent
from app.models.auth import ApiKey
from app.models.subscriber import Subscriber
from app.services import api_key_administration as owner
from app.services import auth_dependencies as deps
from app.services.db_session_adapter import db_session_adapter
from tests.support.tx_trace import assert_protected_tx, trace_transactions

RAW = "inert-presented-key"
LEGACY = "inert-legacy-digest"
PREFERRED = "hmac256:inert-preferred-digest"
NOW = datetime(2026, 10, 12, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def local_auth_owner_session(db_session, monkeypatch):
    """Separate lifecycle, shared unit connection; never the caller Session."""

    @contextmanager
    def fresh():
        with Session(
            bind=db_session.get_bind(), join_transaction_mode="create_savepoint"
        ) as command_db:
            yield command_db
            assert not command_db.in_transaction()

    monkeypatch.setattr(db_session_adapter, "owner_command_session", fresh)


@pytest.fixture
def inert_hashes(monkeypatch):
    monkeypatch.setattr(
        "app.services.auth.hash_api_key_candidates",
        lambda raw: [PREFERRED, LEGACY] if raw == RAW else ["unmatched"],
    )


@pytest.fixture
def clock(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(owner, "datetime", Clock)


def make_key(db, *, legacy=True, last_used=None, scopes=("audit:read",), **fields):
    row = ApiKey(
        key_hash=LEGACY if legacy else PREFERRED,
        label="excluded-key-label",
        scopes=list(scopes),
        is_active=True,
        expires_at=NOW + timedelta(days=1),
        last_used_at=last_used,
        **fields,
    )
    db.add(row)
    db.flush()
    key_id = row.id
    db.commit()
    return key_id


def command(audience=owner.ApiKeyAuthenticationAudience.GENERAL):
    return owner.AuthenticateApiKeyCommand(RAW, audience)


def snapshot(db, key_id):
    db.expire_all()
    row = db.get(ApiKey, key_id)
    assert row is not None
    result = (row.key_hash, row.last_used_at, tuple(row.scopes or ()))
    db.commit()
    return result


def test_upgrade_and_audit_share_root(db_session, inert_hashes, clock):
    target = make_key(db_session)
    with trace_transactions(db_session.get_bind().engine) as trace:
        result = owner.authenticate(db_session, command=command())
    assert_protected_tx(trace.result, protected_tables=frozenset({"api_keys"}))
    assert result.principal == owner.AuthenticatedApiKey(target, None, ("audit:read",))
    assert snapshot(db_session, target)[:2] == (PREFERRED, NOW.replace(tzinfo=None))
    audits = db_session.scalars(select(AuditEvent)).all()
    assert len(audits) == 1
    audit = audits[0]
    assert audit.action == "auth.api_key.hash_upgraded"
    assert audit.actor_type.value == "api_key" and audit.actor_id == str(target)
    assert audit.entity_id == str(target)
    assert set(audit.details) == {
        "schema_version",
        "old_scheme",
        "new_scheme",
        "scope",
        "reason",
        "command_id",
        "correlation_id",
    }
    assert (
        audit.details["old_scheme"] == "sha256"
        and audit.details["new_scheme"] == "hmac256"
    )
    assert audit.details["scope"] == "api_key:authenticate"
    assert audit.details["reason"] == "local_api_key_authentication_maintenance"
    assert not any(
        value in repr(audit.details) + repr(command())
        for value in (RAW, LEGACY, PREFERRED, "excluded-key-label", "audit:read")
    )


@pytest.mark.parametrize("legacy", [False, True])
def test_hot_auth_has_zero_updates_and_audits(
    db_session, inert_hashes, clock, monkeypatch, legacy
):
    target = make_key(db_session, legacy=legacy, last_used=NOW - timedelta(minutes=4))
    if legacy:
        # Compatibility when no HMAC secret exists: preferred remains SHA.
        monkeypatch.setattr(
            "app.services.auth.hash_api_key_candidates", lambda raw: [LEGACY]
        )
    updates = []

    def observe(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("UPDATE"):
            updates.append(True)

    event.listen(db_session.get_bind(), "before_cursor_execute", observe)
    try:
        assert (
            owner.authenticate(db_session, command=command()).principal.key_id == target
        )
    finally:
        event.remove(db_session.get_bind(), "before_cursor_execute", observe)
    assert updates == []
    assert db_session.scalars(select(AuditEvent)).all() == []


@pytest.mark.parametrize(
    "age,touched", [(299, False), (300, True), (301, True), (-60, False)]
)
def test_five_minute_boundary_and_future_touch(
    db_session, inert_hashes, clock, age, touched
):
    previous = NOW - timedelta(seconds=age)
    target = make_key(db_session, legacy=False, last_used=previous)
    assert owner.authenticate(db_session, command=command()).principal is not None
    _, stored, _ = snapshot(db_session, target)
    assert stored == (NOW if touched else previous).replace(tzinfo=None)
    assert db_session.scalars(select(AuditEvent)).all() == []


def intercept_cas(db, monkeypatch, mutation):
    original = db.execute
    counts = {"updates": 0, "selects": 0}

    def execute(statement, *args, **kwargs):
        if isinstance(statement, Update) and statement.table.name == "api_keys":
            counts["updates"] += 1
            mutation(original)
        elif getattr(statement, "is_select", False):
            counts["selects"] += 1
        return original(statement, *args, **kwargs)

    monkeypatch.setattr(db, "execute", execute)
    return counts


@pytest.mark.parametrize(
    "mutation,allowed",
    [
        ("rotate", False),
        ("revoke", False),
        ("delete", False),
        ("equivalent", True),
        ("owner_scope", True),
    ],
)
def test_cas_miss_uses_exactly_one_fresh_projection(
    db_session, inert_hashes, clock, monkeypatch, mutation, allowed
):
    target = make_key(db_session)
    subscriber = Subscriber(
        first_name="Inert", last_name="Owner", email=f"inert-{uuid4()}@test.invalid"
    )
    db_session.add(subscriber)
    db_session.flush()
    subscriber_id = subscriber.id
    db_session.commit()

    def mutate(execute):
        from sqlalchemy import delete

        if mutation == "delete":
            execute(delete(ApiKey).where(ApiKey.id == target))
            return
        values = {"key_hash": PREFERRED}
        if mutation == "rotate":
            values = {"key_hash": "hmac256:other-secret"}
        elif mutation == "revoke":
            values = {"is_active": False}
        elif mutation == "owner_scope":
            values.update(subscriber_id=subscriber_id, scopes=["network:read"])
        execute(sql_update(ApiKey).where(ApiKey.id == target).values(**values))

    counts = intercept_cas(db_session, monkeypatch, mutate)
    result = owner.authenticate(db_session, command=command())
    assert bool(result.principal) is allowed
    assert counts == {"updates": 1, "selects": 2}
    if mutation == "owner_scope":
        assert result.principal == owner.AuthenticatedApiKey(
            target, subscriber_id, ("network:read",)
        )
    assert db_session.scalars(select(AuditEvent)).all() == []


def test_timestamp_case_preserves_newer_concurrent_touch(
    db_session, inert_hashes, clock, monkeypatch
):
    target = make_key(db_session, last_used=NOW - timedelta(minutes=10))
    newer = NOW + timedelta(seconds=1)
    intercept_cas(
        db_session,
        monkeypatch,
        lambda execute: execute(
            sql_update(ApiKey).where(ApiKey.id == target).values(last_used_at=newer)
        ),
    )
    assert owner.authenticate(db_session, command=command()).principal is not None
    assert snapshot(db_session, target)[:2] == (PREFERRED, newer.replace(tzinfo=None))


@pytest.mark.parametrize("denial", ["scope", "expiry"])
def test_post_update_denial_rolls_back_maintenance(
    db_session, inert_hashes, clock, monkeypatch, denial
):
    target = make_key(db_session)

    def mutate(execute):
        execute(
            sql_update(ApiKey)
            .where(ApiKey.id == target)
            .values(
                **(
                    {"scopes": ["billing:read"]}
                    if denial == "scope"
                    else {"expires_at": NOW - timedelta(seconds=1)}
                )
            )
        )

    intercept_cas(db_session, monkeypatch, mutate)
    result = owner.authenticate(
        db_session, command=command(owner.ApiKeyAuthenticationAudience.AUDIT)
    )
    assert result.principal is None
    if denial == "scope":
        assert snapshot(db_session, target)[0] == LEGACY
    assert db_session.scalars(select(AuditEvent)).all() == []


@pytest.mark.parametrize("failure", ["audit", "commit"])
def test_failure_never_grants_and_restores_hash(
    db_session, inert_hashes, clock, monkeypatch, failure
):
    target = make_key(db_session)

    def fail(*args, **kwargs):
        raise OperationalError(
            "excluded-sql-secret", {"key": RAW}, Exception("excluded-db-detail")
        )

    if failure == "audit":
        monkeypatch.setattr(owner, "stage_audit_event", fail)
    else:
        event.listen(db_session, "before_commit", fail, once=True)
    with pytest.raises(owner.ApiKeyAuthenticationError) as exc:
        owner.authenticate(db_session, command=command())
    assert RAW not in str(exc.value) and "excluded" not in str(exc.value)
    assert snapshot(db_session, target)[0] == LEGACY
    assert db_session.scalars(select(AuditEvent)).all() == []


@pytest.mark.parametrize(
    "scopes,allowed",
    [
        ((), False),
        (("*",), False),
        (("audit:write",), False),
        (("audit:*",), True),
        (("audit:read",), True),
    ],
)
def test_local_audit_audience_exact_scope(
    db_session, inert_hashes, clock, scopes, allowed
):
    make_key(db_session, scopes=scopes)
    outcome = owner.authenticate(
        db_session, command=command(owner.ApiKeyAuthenticationAudience.AUDIT)
    )
    assert bool(outcome.principal) is allowed


def test_adapter_fresh_session_preserves_pending_caller_writes(
    db_session, inert_hashes, clock
):
    target = make_key(db_session)
    pending = Subscriber(
        first_name="Pending",
        last_name="Unflushed",
        email=f"pending-{uuid4()}@test.invalid",
    )
    db_session.add(pending)
    result = deps._api_key_principal(db_session, RAW, None)
    assert result["api_key_id"] == str(target)
    assert pending in db_session.new
    assert db_session.in_transaction()


def test_audit_adapter_returns_verified_actor(db_session, inert_hashes, clock):
    target = make_key(db_session)
    assert deps.require_audit_auth(
        authorization=None, x_session_token=None, x_api_key=RAW, db=db_session
    ) == {"actor_type": "api_key", "actor_id": str(target)}


def test_adapter_busy_is_sanitized(monkeypatch):
    @contextmanager
    def fresh():
        yield object()

    monkeypatch.setattr(db_session_adapter, "owner_command_session", fresh)

    def fail(*args, **kwargs):
        raise owner.ApiKeyAuthenticationError(
            code=f"{owner.OWNER}.authentication_busy", message="safe"
        )

    monkeypatch.setattr(owner, "authenticate", fail)
    with pytest.raises(HTTPException) as exc:
        deps._authenticate_local_api_key(
            RAW, audience=owner.ApiKeyAuthenticationAudience.GENERAL
        )
    assert exc.value.status_code == 503 and exc.value.headers == {"Retry-After": "5"}


def test_command_has_no_claimed_actor_or_target_and_invalid_audience_refuses(
    db_session,
):
    from dataclasses import fields

    assert {item.name for item in fields(owner.AuthenticateApiKeyCommand)} == {
        "raw_key",
        "audience",
        "provenance",
    }
    with pytest.raises(owner.ApiKeyAuthenticationError):
        owner.authenticate(db_session, command=replace(command(), audience="admin"))


def test_successful_cas_then_wall_clock_expiry_rolls_back(
    db_session, inert_hashes, clock, monkeypatch
):
    target = make_key(db_session)
    original = db_session.execute

    class ExpiredClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW + timedelta(days=2)

    def execute(statement, *args, **kwargs):
        result = original(statement, *args, **kwargs)
        if isinstance(statement, Update) and statement.table.name == "api_keys":
            monkeypatch.setattr(owner, "datetime", ExpiredClock)
        return result

    monkeypatch.setattr(db_session, "execute", execute)
    assert owner.authenticate(db_session, command=command()).principal is None
    assert snapshot(db_session, target)[:2] == (LEGACY, None)
    assert db_session.scalars(select(AuditEvent)).all() == []


def test_failed_machine_probe_releases_clean_connection_before_local_owner(
    monkeypatch, tmp_path
):
    from sqlalchemy import create_engine, text
    from sqlalchemy.pool import QueuePool

    engine = create_engine(
        f"sqlite:///{tmp_path / 'bounded-pool.db'}",
        poolclass=QueuePool,
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.1,
    )

    @contextmanager
    def fresh():
        with Session(engine) as db:
            yield db

    def machine_miss(db, raw, request):
        assert db.scalar(text("SELECT 1")) == 1
        return None

    def owner_result(db, *, command):
        assert db.scalar(text("SELECT 1")) == 1
        db.commit()
        return owner.ApiKeyAuthenticationOutcome(None)

    monkeypatch.setattr(deps, "_machine_principal", machine_miss)
    monkeypatch.setattr(db_session_adapter, "owner_command_session", fresh)
    monkeypatch.setattr(owner, "authenticate", owner_result)
    try:
        with Session(engine) as caller:
            assert deps._api_key_principal(caller, RAW, None) is None
            assert not caller.in_transaction()
    finally:
        engine.dispose()


@pytest.mark.parametrize("audit", [False, True])
def test_adapter_preserves_flushed_caller_transaction(
    db_session, inert_hashes, clock, monkeypatch, audit
):
    target = make_key(db_session)
    pending = Subscriber(
        first_name="Flushed",
        last_name="Uncommitted",
        email=f"flushed-{uuid4()}@test.invalid",
    )
    db_session.add(pending)
    db_session.flush()
    assert not db_session.new and not db_session.dirty
    commits = []
    event.listen(db_session, "before_commit", lambda db: commits.append(True))
    if audit:
        result = deps.require_audit_auth(
            authorization=None, x_session_token=None, x_api_key=RAW, db=db_session
        )
        assert result["actor_id"] == str(target)
    else:
        result = deps._api_key_principal(db_session, RAW, None)
        assert result["api_key_id"] == str(target)
    assert db_session.in_transaction() and commits == []
    assert db_session.get(Subscriber, pending.id) is pending
