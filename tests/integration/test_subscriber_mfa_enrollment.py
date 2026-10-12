"""Migrated PostgreSQL-only enrollment serialization and stale-standing proofs.

Collect-only on workstations. Hosted make test-integration owns execution against
an explicitly disposable database migrated through the real Alembic head.
Reset winner-order proofs are in test_customer_mfa_reset.py.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from queue import Queue
from types import SimpleNamespace
from uuid import uuid4

import pyotp
import pytest
from sqlalchemy import event, select, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session, SessionTransaction, sessionmaker

from app import subscriber_mfa_enrollment_http as adapter
from app.models.auth import MFAMethod, SessionStatus
from app.models.auth import Session as AuthSession
from app.models.subscriber import Subscriber
from app.services import password_authentication as owner
from tests.integration.test_password_auth_credential_race import (
    _signing as _signing,
)
from tests.integration.test_password_auth_credential_race import (
    account as _account_fixture,
)
from tests.integration.test_password_auth_credential_race import (
    factory as _factory_fixture,
)

account = _account_fixture
factory = _factory_fixture

pytestmark = pytest.mark.integration


def _wait_until_a_session_is_blocked(
    engine: Engine, *, waiter_pid: int, blocker_pid: int
) -> None:
    deadline = time.monotonic() + 10
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as observer:
        while time.monotonic() < deadline:
            if observer.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname=current_database() AND wait_event_type='Lock' "
                    "AND pid=:waiter_pid AND :blocker_pid=ANY(pg_blocking_pids(pid))"
                ),
                {"waiter_pid": waiter_pid, "blocker_pid": blocker_pid},
            ):
                return
            time.sleep(0.05)
    pytest.fail("no enrollment session blocked on a lock")


def _capture_pid(db: Session, pids: Queue[int] | None) -> None:
    if pids is None:
        return

    def after_begin(
        session: Session, transaction: SessionTransaction, connection: Connection
    ) -> None:
        pid = connection.scalar(text("SELECT pg_backend_pid()"))
        assert isinstance(pid, int)
        pids.put(pid)

    event.listen(db, "after_begin", after_begin, once=True)


@pytest.fixture
def enrollment(
    factory: sessionmaker[Session],
    account: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> owner.SetupSubscriberMfaCommand:
    from app.services import events

    real_emit = events.emit_event

    def emit(*args, **kwargs):
        kwargs["dispatch_after_commit"] = False
        return real_emit(*args, **kwargs)

    monkeypatch.setattr(events, "emit_event", emit)
    session_id = uuid4()
    with factory() as db:
        db.add(
            AuthSession(
                id=session_id,
                subscriber_id=account.subscriber_id,
                status=SessionStatus.active,
                token_hash=uuid4().hex,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        db.commit()
    proof = owner.SubscriberMfaSessionProof(session_id, account.subscriber_id, False)
    return owner.SetupSubscriberMfaCommand(
        context=adapter.command_context(proof=proof, reason="PG enrollment proof"),
        subscriber_id=account.subscriber_id,
        label="Authenticator",
        provenance=owner.SubscriberMfaProvenance.CUSTOMER_WEB,
        session=proof,
    )


def _setup(
    factory: sessionmaker[Session],
    command: owner.SetupSubscriberMfaCommand,
    pids: Queue[int] | None = None,
) -> owner.SubscriberMfaSetupOutcome:
    with factory() as db:
        _capture_pid(db, pids)
        return owner.setup_subscriber_mfa(db, command=command)


def _confirm(
    factory: sessionmaker[Session],
    command: owner.SetupSubscriberMfaCommand,
    setup: owner.SubscriberMfaSetupOutcome,
    pids: Queue[int] | None = None,
) -> owner.SubscriberMfaConfirmationOutcome:
    with factory() as db:
        _capture_pid(db, pids)
        return owner.confirm_subscriber_mfa(
            db,
            command=owner.ConfirmSubscriberMfaCommand(
                context=replace(command.context, command_id=uuid4()),
                subscriber_id=command.subscriber_id,
                method_id=setup.method_id,
                code=pyotp.TOTP(setup.secret).now(),
                provenance=command.provenance,
                session=command.session,
                recovery_delivery=owner.RecoveryCodeDelivery.NONE,
            ),
        )


@pytest.mark.parametrize("transition", ["setup", "confirm"])
def test_simultaneous_enrollment_transitions_serialize(
    factory: sessionmaker[Session],
    engine: Engine,
    enrollment: owner.SetupSubscriberMfaCommand,
    monkeypatch: pytest.MonkeyPatch,
    transition: str,
) -> None:
    first_setup = _setup(factory, enrollment)
    if transition == "confirm":
        _confirm(factory, enrollment, first_setup)
        second_setup = _setup(factory, enrollment)
    else:
        second_setup = first_setup
    reached, resume = threading.Event(), threading.Event()
    real_stage = owner._stage_subscriber_enrollment

    def stage(*args, **kwargs) -> None:
        if threading.current_thread().name.startswith("enrollment-winner"):
            reached.set()
            assert resume.wait(20), "enrollment pause not released"
        real_stage(*args, **kwargs)

    monkeypatch.setattr(owner, "_stage_subscriber_enrollment", stage)

    first_pids, second_pids = Queue[int](), Queue[int]()

    def run(setup: owner.SubscriberMfaSetupOutcome, pids: Queue[int]):
        return (
            _setup(factory, enrollment, pids)
            if transition == "setup"
            else _confirm(factory, enrollment, setup, pids)
        )

    with (
        ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="enrollment-winner"
        ) as first_pool,
        ThreadPoolExecutor(max_workers=1) as second_pool,
    ):
        first = first_pool.submit(run, first_setup, first_pids)
        assert reached.wait(20)
        second = second_pool.submit(run, second_setup, second_pids)
        try:
            _wait_until_a_session_is_blocked(
                engine,
                waiter_pid=second_pids.get(timeout=20),
                blocker_pid=first_pids.get(timeout=20),
            )
        finally:
            resume.set()
        left, right = first.result(timeout=20), second.result(timeout=20)
    with factory() as db:
        methods = list(
            db.scalars(
                select(MFAMethod).where(
                    MFAMethod.subscriber_id == enrollment.subscriber_id
                )
            )
        )
        if transition == "setup":
            assert isinstance(left, owner.SubscriberMfaSetupOutcome) and isinstance(
                right, owner.SubscriberMfaSetupOutcome
            )
            assert left.method_id == right.method_id and len(methods) == 1
        else:
            assert sum(method.is_primary for method in methods) == 1
            assert all(method.enabled for method in methods)


@pytest.mark.parametrize("transition", ["setup", "confirm"])
def test_revocation_defeats_stale_identity_map(
    factory: sessionmaker[Session],
    enrollment: owner.SetupSubscriberMfaCommand,
    transition: str,
) -> None:
    setup = _setup(factory, enrollment)
    with factory() as stale:
        cached = stale.get(AuthSession, enrollment.session.session_id)
        assert cached is not None and cached.status is SessionStatus.active
        stale.commit()  # expire_on_commit=False preserves the stale ORM observation.
        with factory() as revoker:
            session = revoker.get(AuthSession, enrollment.session.session_id)
            assert session is not None
            session.status, session.revoked_at = (
                SessionStatus.revoked,
                datetime.now(UTC),
            )
            revoker.commit()
        with pytest.raises(owner.PasswordAuthenticationError) as exc:
            if transition == "setup":
                owner.setup_subscriber_mfa(stale, command=enrollment)
            else:
                owner.confirm_subscriber_mfa(
                    stale,
                    command=owner.ConfirmSubscriberMfaCommand(
                        context=enrollment.context,
                        subscriber_id=enrollment.subscriber_id,
                        method_id=setup.method_id,
                        code=pyotp.TOTP(setup.secret).now(),
                        provenance=enrollment.provenance,
                        session=enrollment.session,
                        recovery_delivery=owner.RecoveryCodeDelivery.NONE,
                    ),
                )
        assert exc.value.kind == "invalid_mfa_session" and not stale.in_transaction()


def test_principal_lock_timeout_is_typed_and_rolls_back(
    factory: sessionmaker[Session],
    engine: Engine,
    enrollment: owner.SetupSubscriberMfaCommand,
) -> None:
    with factory() as blocker:
        blocker.scalar(
            select(Subscriber)
            .where(Subscriber.id == enrollment.subscriber_id)
            .with_for_update()
        )
        blocker_pid = blocker.scalar(text("SELECT pg_backend_pid()"))
        assert isinstance(blocker_pid, int)
        pids = Queue[int]()
        with ThreadPoolExecutor(max_workers=1) as pool:
            blocked = pool.submit(_setup, factory, enrollment, pids)
            _wait_until_a_session_is_blocked(
                engine, waiter_pid=pids.get(timeout=20), blocker_pid=blocker_pid
            )
            with pytest.raises(owner.PasswordAuthenticationError) as exc:
                blocked.result(timeout=15)
            assert exc.value.kind == "lock_timeout" and exc.value.retryable
        blocker.rollback()


@pytest.mark.parametrize("transition", ["setup", "confirm"])
def test_real_refresh_releases_session_without_later_principal_lock(
    factory: sessionmaker[Session],
    engine: Engine,
    enrollment: owner.SetupSubscriberMfaCommand,
    monkeypatch: pytest.MonkeyPatch,
    transition: str,
) -> None:
    from app.services import auth_session_refresh as refresh
    from app.services.owner_commands import CommandContext

    setup = _setup(factory, enrollment)
    refresh_token = uuid4().hex
    with factory() as seed:
        session = seed.get(AuthSession, enrollment.session.session_id)
        assert session is not None
        session.token_hash = refresh.hash_refresh_token(refresh_token)
        seed.commit()
    reached, resume = threading.Event(), threading.Event()
    first_pids, second_pids = Queue[int](), Queue[int]()
    real_emit = refresh.emit_event

    def emit(*args, **kwargs):
        reached.set()
        assert resume.wait(20), "refresh pause not released"
        return real_emit(*args, **kwargs)

    monkeypatch.setattr(refresh, "emit_event", emit)

    def renew() -> refresh.RefreshSessionOutcome:
        with factory() as db:
            _capture_pid(db, first_pids)
            return refresh.renew_authentication_session(
                db=db,
                command=refresh.RefreshSessionCommand(
                    context=CommandContext.system(
                        actor=f"subscriber:{enrollment.subscriber_id}",
                        scope="authentication:session",
                        reason="Enrollment/refresh ordering proof",
                    ),
                    refresh_token=refresh_token,
                    client_ip="203.0.113.7",
                    user_agent="enrollment-proof",
                ),
            )

    with (
        ThreadPoolExecutor(max_workers=1) as first_pool,
        ThreadPoolExecutor(max_workers=1) as second_pool,
    ):
        first = first_pool.submit(renew)
        assert reached.wait(20)
        second = (
            second_pool.submit(_setup, factory, enrollment, second_pids)
            if transition == "setup"
            else second_pool.submit(_confirm, factory, enrollment, setup, second_pids)
        )
        try:
            _wait_until_a_session_is_blocked(
                engine,
                waiter_pid=second_pids.get(timeout=20),
                blocker_pid=first_pids.get(timeout=20),
            )
        finally:
            resume.set()
        assert first.result(timeout=20).accepted
        result = second.result(timeout=20)
        assert isinstance(
            result,
            (owner.SubscriberMfaSetupOutcome, owner.SubscriberMfaConfirmationOutcome),
        )
