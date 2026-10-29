"""Hosted PostgreSQL authority: real-Alembic clone, independent connections.

No metadata-built schema or SQLite fallback. Lock waits are proven through
pg_blocking_pids; all barriers, statement deadlines and worker joins are bounded.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta, tzinfo
from threading import Event
from time import monotonic
from uuid import UUID, uuid4

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, event, select, text, update
from sqlalchemy.engine import URL, Connection, Engine
from sqlalchemy.orm import Session

from app.models.catalog import AccessCredential
from app.models.event_store import EventStatus, EventStore
from app.models.projection_secret_request import ProjectionSecretRequest
from app.models.subscriber import Subscriber
from app.services.credential_crypto import encrypt_credential
from app.services.events.types import EventType
from app.services.integrations import projection_secret_fetch as owner
from app.services.owner_commands import CommandContext

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
SECRET = "dummy-pg-projection-secret"


@pytest.fixture
def engine(cloned_database: Callable[[str], URL]) -> Iterator[Engine]:
    value = create_engine(cloned_database("heads"))

    @event.listens_for(value, "begin")
    def deadline(connection: Connection) -> None:
        connection.exec_driver_sql("SET LOCAL statement_timeout = '12s'")

    try:
        yield value
    finally:
        value.dispose()


@pytest.fixture
def command(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> owner.RedeemProjectionSecretCommand:
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    with Session(engine) as db:
        subscriber = Subscriber(
            first_name="Dummy",
            last_name="Projection",
            email=f"projection-{uuid4().hex}@example.invalid",
        )
        db.add(subscriber)
        db.flush()
        credential = AccessCredential(
            subscriber_id=subscriber.id,
            username=f"dummy-{uuid4().hex}@radius",
            secret_hash=encrypt_credential(SECRET),
            is_active=True,
            updated_at=NOW,
        )
        db.add(credential)
        db.flush()
        request = owner.open_projection_request(db, credential=credential, now=NOW)
        result = owner.RedeemProjectionSecretCommand(
            context=CommandContext.system(
                actor="test:projection-secret",
                scope=owner.PROJECTION_SECRET_FETCH_SCOPE,
                reason="prove atomic single-use release on migrated PostgreSQL",
            ),
            request_ref=request.request_ref,
            credential_id=credential.id,
            secret_version=request.secret_version,
            now=NOW + timedelta(minutes=1),
        )
        db.commit()
    return result


@dataclass
class Worker:
    ready: Event = field(default_factory=Event)
    release: Event = field(default_factory=Event)
    backend_pid: int | None = None

    def pause_commit(self, db: Session) -> None:
        db.flush()
        self.ready.set()
        assert self.release.wait(8), "owner commit was not released"


def _redeem(
    engine: Engine,
    command: owner.RedeemProjectionSecretCommand,
    worker: Worker,
    *,
    pause_commit: bool = False,
) -> str:
    # Retain the physical connection across identity-read completion and command.
    with engine.connect() as connection, Session(connection) as db:
        worker.backend_pid = db.scalar(text("SELECT pg_backend_pid()"))
        db.commit()
        if pause_commit:
            event.listen(db, "before_commit", worker.pause_commit)
        else:
            worker.ready.set()
        try:
            result = owner.redeem_projection_secret(db, command=command)
            assert result.secret == SECRET and not db.in_transaction()
            return "released"
        except owner.SecretFetchRefused as error:
            assert str(error) == "projection secret fetch refused"
            assert error.code == "access.projection_secret_fetch.refused"
            assert error.details == {} and not db.in_transaction()
            return "refused"


def _blocked_by(engine: Engine, waiter: Worker, blocker: int | None) -> None:
    assert waiter.ready.wait(3) and waiter.backend_pid is not None
    assert blocker is not None
    deadline = monotonic() + 3
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as observer:
        while monotonic() < deadline:
            blockers = observer.scalar(
                text("SELECT pg_blocking_pids(:pid)"), {"pid": waiter.backend_pid}
            )
            if blocker in blockers:
                return
    raise AssertionError("redemption did not wait for the exact lock holder")


def _assert_effects(engine: Engine, *, released: bool) -> None:
    with Session(engine) as db:
        requests = db.scalars(select(ProjectionSecretRequest)).all()
        assert len(requests) == 1
        assert (requests[0].redeemed_at is not None) is released
        events = db.scalars(
            select(EventStore).where(
                EventStore.event_type
                == EventType.access_projection_secret_revealed.value
            )
        ).all()
        assert len(events) == int(released)
        if events:
            assert events[0].status is EventStatus.pending
            assert events[0].actor == "test:projection-secret"
            assert events[0].payload["request_ref"] == requests[0].request_ref
            assert SECRET not in str(events[0].payload)
            assert "secret" not in events[0].payload


def test_two_sessions_release_once_after_exact_lock_wait(
    engine: Engine,
    command: owner.RedeemProjectionSecretCommand,
) -> None:
    first, second = Worker(), Worker()
    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(_redeem, engine, command, first, pause_commit=True)
        try:
            assert first.ready.wait(3)
            two = pool.submit(_redeem, engine, command, second)
            _blocked_by(engine, second, first.backend_pid)
        finally:
            first.release.set()
        assert one.result(timeout=15) == "released"
        assert two.result(timeout=15) == "refused"
    _assert_effects(engine, released=True)


@pytest.mark.parametrize(
    "failure", ["decrypt", "empty_decrypt", "event", "before_commit_abort"]
)
def test_failure_rolls_back_then_retry_releases_once(
    engine: Engine,
    command: owner.RedeemProjectionSecretCommand,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("inert injected failure")

    with monkeypatch.context() as patch, Session(engine) as db:
        if failure == "decrypt":
            patch.setattr(owner, "decrypt_credential_with_key", fail)
        elif failure == "empty_decrypt":
            patch.setattr(owner, "decrypt_credential_with_key", lambda *_: "")
        elif failure == "event":
            original = owner.emit_event

            def fail_after_staging(
                db: Session,
                event_type: EventType,
                payload: dict[str, object],
                *,
                actor: str,
                account_id: UUID,
                subscription_id: UUID | None,
                dispatch_after_commit: bool,
            ) -> None:
                original(
                    db,
                    event_type,
                    payload,
                    actor=actor,
                    account_id=account_id,
                    subscription_id=subscription_id,
                    dispatch_after_commit=dispatch_after_commit,
                )
                raise RuntimeError("inert injected failure")

            patch.setattr(owner, "emit_event", fail_after_staging)
        else:
            event.listen(db, "before_commit", fail)
        expected = (
            owner.SecretFetchRefused
            if failure in {"decrypt", "empty_decrypt"}
            else RuntimeError
        )
        with pytest.raises(expected):
            owner.redeem_projection_secret(db, command=command)
        assert not db.in_transaction()
    _assert_effects(engine, released=False)
    assert _redeem(engine, command, Worker()) == "released"
    assert _redeem(engine, command, Worker()) == "refused"
    _assert_effects(engine, released=True)


@pytest.mark.parametrize("change", ["rotation", "inactive", "spent"])
def test_retained_identity_is_refreshed_before_release(
    engine: Engine,
    command: owner.RedeemProjectionSecretCommand,
    change: str,
) -> None:
    with Session(engine, expire_on_commit=False) as stale:
        credential = stale.get(AccessCredential, command.credential_id)
        request = stale.scalar(select(ProjectionSecretRequest))
        assert credential is not None and request is not None
        stale.commit()
        if change == "spent":
            assert _redeem(engine, command, Worker()) == "released"
        else:
            with Session(engine) as writer:
                values = (
                    {"updated_at": NOW + timedelta(minutes=2)}
                    if change == "rotation"
                    else {"is_active": False}
                )
                writer.execute(
                    update(AccessCredential)
                    .where(AccessCredential.id == command.credential_id)
                    .values(**values)
                )
                writer.commit()
        with pytest.raises(owner.SecretFetchRefused):
            owner.redeem_projection_secret(stale, command=command)
    _assert_effects(engine, released=change == "spent")


def test_rotation_lock_wait_refreshes_committed_version(
    engine: Engine,
    command: owner.RedeemProjectionSecretCommand,
) -> None:
    waiter = Worker()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with Session(engine) as holder:
            holder.scalar(
                select(AccessCredential)
                .where(AccessCredential.id == command.credential_id)
                .with_for_update()
            )
            blocker = holder.scalar(text("SELECT pg_backend_pid()"))
            future = pool.submit(_redeem, engine, command, waiter)
            try:
                _blocked_by(engine, waiter, blocker)
                holder.execute(
                    update(AccessCredential)
                    .where(AccessCredential.id == command.credential_id)
                    .values(updated_at=NOW + timedelta(minutes=2))
                )
                holder.commit()
            finally:
                holder.rollback()
        assert future.result(timeout=15) == "refused"
    _assert_effects(engine, released=False)


@pytest.mark.parametrize(
    "change", ["expired", "wrong_binding", "wrong_version", "unknown", "wrong_scope"]
)
def test_opaque_refusal_has_no_burn_or_event(
    engine: Engine,
    command: owner.RedeemProjectionSecretCommand,
    change: str,
) -> None:
    if change == "expired":
        command = replace(command, now=NOW + timedelta(hours=1))
    elif change == "wrong_binding":
        command = replace(command, credential_id=uuid4())
    elif change == "wrong_version":
        command = replace(command, secret_version=command.secret_version + 1)
    elif change == "wrong_scope":
        command = replace(
            command, context=replace(command.context, scope="test:wrong-scope")
        )
    else:
        command = replace(command, request_ref="projection-request:unknown")
    assert _redeem(engine, command, Worker()) == "refused"
    _assert_effects(engine, released=False)


def test_redemption_holds_credential_lock_until_release_commit(
    engine: Engine,
    command: owner.RedeemProjectionSecretCommand,
) -> None:
    first, rotation = Worker(), Worker()

    def rotate() -> None:
        with engine.connect() as connection, Session(connection) as db:
            rotation.backend_pid = db.scalar(text("SELECT pg_backend_pid()"))
            db.commit()
            rotation.ready.set()
            db.execute(
                update(AccessCredential)
                .where(AccessCredential.id == command.credential_id)
                .values(updated_at=NOW + timedelta(minutes=2))
            )
            db.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        release = pool.submit(_redeem, engine, command, first, pause_commit=True)
        try:
            assert first.ready.wait(3)
            rotated = pool.submit(rotate)
            _blocked_by(engine, rotation, first.backend_pid)
        finally:
            first.release.set()
        assert release.result(timeout=15) == "released"
        rotated.result(timeout=15)
    assert _redeem(engine, command, Worker()) == "refused"
    _assert_effects(engine, released=True)


def test_expiry_crossing_exact_lock_wait_refuses_without_release(
    engine: Engine,
    command: owner.RedeemProjectionSecretCommand,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A pre-lock clock read would capture the valid time and wrongly release
    # after the observed lock wait has crossed the authorization's expiry.
    clock_value = NOW + timedelta(minutes=1)

    class ControlledDatetime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return (
                clock_value.astimezone(tz)
                if tz is not None
                else clock_value.replace(tzinfo=None)
            )

    monkeypatch.setattr(owner, "datetime", ControlledDatetime)
    command = replace(command, now=None)
    waiter = Worker()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with Session(engine) as holder:
            holder.scalar(
                select(AccessCredential)
                .where(AccessCredential.id == command.credential_id)
                .with_for_update()
            )
            blocker = holder.scalar(text("SELECT pg_backend_pid()"))
            future = pool.submit(_redeem, engine, command, waiter)
            try:
                _blocked_by(engine, waiter, blocker)
                clock_value = NOW + owner.REQUEST_TTL + timedelta(seconds=1)
                holder.commit()
            finally:
                holder.rollback()
        assert future.result(timeout=15) == "refused"
    _assert_effects(engine, released=False)
