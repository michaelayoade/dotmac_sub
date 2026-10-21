"""Independent-connection lock proofs on a disposable real-Alembic-all-heads clone.

Collected locally only. Hosted PostgreSQL execution is the concurrency authority.
Each wait is observed through pg_blocking_pids for the exact backend pair.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from threading import Event
from time import monotonic
from uuid import UUID

import pytest
from sqlalchemy import create_engine, event, select, text, update
from sqlalchemy.engine import URL, Connection, Engine
from sqlalchemy.orm import Session

from app.models.audit import AuditEvent
from app.models.auth import (
    AuthenticationBinding,
    AuthProvider,
    SessionStatus,
    UserCredential,
)
from app.models.auth import Session as AuthSession
from app.models.oidc_mobile import OidcCeremonyOutcome, OidcMobileCeremony
from app.models.system_user import SystemUser
from app.services import auth_flow, credential_recovery
from app.services import oidc_mobile_federation as federation
from app.services import oidc_mobile_verifier as verifier
from app.services.owner_commands import CommandContext
from tests.oidc_mobile_fixtures import (
    CLIENT_ID,
    REDIRECT_URI,
    RecordingTransport,
    SigningKey,
    assertion_claims,
    bind_field_technician,
    configure_federation,
    install_oidc_binding,
)
from tests.test_oidc_mobile_federation import _request, _start


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


@dataclass(frozen=True)
class Scenario:
    principal_id: UUID
    credential_id: UUID
    binding_id: UUID
    local_credential_id: UUID
    reset_token: str = field(repr=False)
    key: SigningKey = field(repr=False)


@pytest.fixture
def scenario(engine: Engine) -> Iterator[Scenario]:
    key = SigningKey.generate()
    previous = verifier.install_transport(RecordingTransport([key.public_jwk]))
    verifier.reset_verifiers()
    try:
        with Session(engine) as db:
            configure_federation(db)
            binding = install_oidc_binding(db)
            user, _, credential = bind_field_technician(db, binding)
            principal_id, credential_id = user.id, credential.id
            local = UserCredential(
                system_user_id=principal_id,
                provider=AuthProvider.local,
                username=user.email,
                password_hash=auth_flow.hash_password("Original-Atomic-Password-44!"),
                is_active=True,
            )
            db.add(local)
            db.flush()
            local_id = local.id
            capability = credential_recovery.issue_exact_reset_capability(
                db,
                principal_type="system_user",
                principal_id=principal_id,
            )
            assert capability is not None
            db.commit()
        yield Scenario(
            principal_id,
            credential_id,
            binding.binding_id,
            local_id,
            capability.token,
            key,
        )
    finally:
        verifier.install_transport(previous)
        verifier.reset_verifiers()


@dataclass
class Worker:
    ready: Event = field(default_factory=Event)
    release: Event = field(default_factory=Event)
    backend_pid: int | None = None

    def identify(self, db: Session) -> None:
        self.backend_pid = db.scalar(text("SELECT pg_backend_pid()"))
        db.commit()

    def pause_before_commit(self, db: Session) -> None:
        db.flush()
        self.ready.set()
        assert self.release.wait(8), "test did not release the owner commit"


def _blocked_by(engine: Engine, *, waiter: Worker, blocker: int) -> None:
    assert waiter.ready.wait(3) and waiter.backend_pid is not None
    deadline = monotonic() + 3
    # A bounded poll observes the actual graph, never assumes a lock after sleep.
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as observer:
        while monotonic() < deadline:
            blockers = observer.scalar(
                text("SELECT pg_blocking_pids(:pid)"), {"pid": waiter.backend_pid}
            )
            if blocker in blockers:
                return
    raise AssertionError(f"backend {waiter.backend_pid} never waited for {blocker}")


def _start_assertion(engine: Engine, scenario: Scenario) -> tuple[UUID, str]:
    with Session(engine) as db:
        started = _start(db)
    return started.ceremony_id, scenario.key.sign(assertion_claims(started.nonce))


def _exchange_worker(
    engine: Engine,
    ceremony: UUID,
    token: str,
    worker: Worker,
    *,
    pause_commit: bool = False,
    device: str | None = None,
) -> str:
    # Keep this physical connection through identify/commit and the owner call;
    # a pooled Session(engine) could acquire a different backend after commit.
    with engine.connect() as connection, Session(connection) as db:
        worker.identify(db)
        if pause_commit:
            event.listen(db, "before_commit", worker.pause_before_commit)
        else:
            worker.ready.set()
        try:
            issued = federation.exchange_mobile_assertion(
                db,
                federation.ExchangeMobileAssertionCommand(
                    context=CommandContext.system(
                        actor="test:oidc-concurrency",
                        scope=federation.OIDC_MOBILE_FEDERATION_SCOPE,
                        reason="verify exact atomic lock graph",
                    ),
                    ceremony_id=ceremony,
                    id_token=token,
                    client_id=CLIENT_ID,
                    redirect_uri=REDIRECT_URI,
                ),
                request=_request(device),
            )
            assert issued.access_token and not db.in_transaction()
            return "issued"
        except federation.OidcFederationRefused as refused:
            return refused.reason


@pytest.mark.parametrize(
    "change", ["binding_disabled", "candidate_disabled", "candidate_version"]
)
def test_principal_first_wait_refreshes_changed_admission_facts(
    engine: Engine,
    scenario: Scenario,
    change: str,
) -> None:
    ceremony, token = _start_assertion(engine, scenario)
    waiter = Worker()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with Session(engine) as holder:
            holder.scalar(
                select(SystemUser)
                .where(SystemUser.id == scenario.principal_id)
                .with_for_update()
            )
            blocker = holder.scalar(text("SELECT pg_backend_pid()"))
            future = pool.submit(_exchange_worker, engine, ceremony, token, waiter)
            try:
                _blocked_by(engine, waiter=waiter, blocker=blocker)
                # If exchange held credential/binding before principal these writes
                # would form the inverse cycle; this connection owns principal first.
                if change == "binding_disabled":
                    holder.execute(
                        update(AuthenticationBinding)
                        .where(AuthenticationBinding.id == scenario.binding_id)
                        .values(is_active=False)
                    )
                elif change == "candidate_disabled":
                    holder.execute(
                        update(UserCredential)
                        .where(UserCredential.id == scenario.credential_id)
                        .values(is_active=False)
                    )
                else:
                    holder.execute(
                        update(UserCredential)
                        .where(UserCredential.id == scenario.credential_id)
                        .values(
                            credential_version=UserCredential.credential_version + 1
                        )
                    )
                holder.commit()
            finally:
                holder.rollback()
        assert future.result(timeout=15) == "principal_not_eligible"
    with Session(engine) as verify:
        row = verify.get(OidcMobileCeremony, ceremony)
        assert row.outcome == OidcCeremonyOutcome.failed.value and row.consumed_at
        assert verify.query(AuthSession).count() == 0
        assert (
            verify.query(AuditEvent)
            .filter_by(action="auth.oidc_mobile_assertion_admitted")
            .count()
            == 0
        )


def test_duplicate_ceremony_waits_for_exact_first_owner_and_issues_once(
    engine: Engine,
    scenario: Scenario,
) -> None:
    ceremony, token = _start_assertion(engine, scenario)
    first, second = Worker(), Worker()
    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(
            _exchange_worker, engine, ceremony, token, first, pause_commit=True
        )
        try:
            assert first.ready.wait(3)
            two = pool.submit(_exchange_worker, engine, ceremony, token, second)
            _blocked_by(engine, waiter=second, blocker=first.backend_pid)
        finally:
            first.release.set()
        assert one.result(timeout=15) == "issued"
        assert two.result(timeout=15) == "ceremony_already_used"
    with Session(engine) as verify:
        assert verify.query(AuthSession).count() == 1
        assert (
            verify.query(AuditEvent)
            .filter_by(action="auth.oidc_mobile_assertion_admitted")
            .count()
            == 1
        )


def test_distinct_ceremonies_same_device_serialize_at_principal(
    engine: Engine,
    scenario: Scenario,
) -> None:
    ceremony_one, token_one = _start_assertion(engine, scenario)
    ceremony_two, token_two = _start_assertion(engine, scenario)
    first, second = Worker(), Worker()
    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(
            _exchange_worker,
            engine,
            ceremony_one,
            token_one,
            first,
            pause_commit=True,
            device="same-device",
        )
        try:
            assert first.ready.wait(3)
            two = pool.submit(
                _exchange_worker,
                engine,
                ceremony_two,
                token_two,
                second,
                device="same-device",
            )
            _blocked_by(engine, waiter=second, blocker=first.backend_pid)
        finally:
            first.release.set()
        assert one.result(timeout=15) == two.result(timeout=15) == "issued"
    with Session(engine) as verify:
        rows = (
            verify.query(AuthSession)
            .filter_by(system_user_id=scenario.principal_id, device_id="same-device")
            .all()
        )
        assert len(rows) == 2
        assert (
            sum(
                row.status is SessionStatus.active and row.revoked_at is None
                for row in rows
            )
            == 1
        )
        assert (
            sum(
                row.status is SessionStatus.revoked and row.revoked_at is not None
                for row in rows
            )
            == 1
        )


def _reset_worker(
    engine: Engine, scenario: Scenario, worker: Worker, *, pause_commit: bool
) -> int:
    # Keep this physical connection through identify/commit and the owner call;
    # a pooled Session(engine) could acquire a different backend after commit.
    with engine.connect() as connection, Session(connection) as db:
        worker.identify(db)
        if pause_commit:
            event.listen(db, "before_commit", worker.pause_before_commit)
        else:
            worker.ready.set()
        result = credential_recovery.complete_password_reset(
            db,
            credential_recovery.CompletePasswordResetCommand(
                context=CommandContext.system(
                    actor="system:pytest",
                    scope=credential_recovery.CREDENTIAL_RECOVERY_SCOPE,
                    reason="exact reset/issuance arbitration",
                ),
                token=scenario.reset_token,
                new_password="Reset-Atomic-Password-55!",
            ),
        )
        return result.sessions_revoked


@pytest.mark.parametrize("first_owner", ["oidc", "reset"])
def test_real_password_reset_and_oidc_serialize_at_the_same_principal(
    engine: Engine,
    scenario: Scenario,
    first_owner: str,
) -> None:
    ceremony, token = _start_assertion(engine, scenario)
    first, second = Worker(), Worker()
    with ThreadPoolExecutor(max_workers=2) as pool:
        if first_owner == "oidc":
            one = pool.submit(
                _exchange_worker, engine, ceremony, token, first, pause_commit=True
            )
        else:
            one = pool.submit(_reset_worker, engine, scenario, first, pause_commit=True)
        try:
            assert first.ready.wait(3)
            if first_owner == "oidc":
                two = pool.submit(
                    _reset_worker, engine, scenario, second, pause_commit=False
                )
            else:
                two = pool.submit(_exchange_worker, engine, ceremony, token, second)
            _blocked_by(engine, waiter=second, blocker=first.backend_pid)
        finally:
            first.release.set()
        first_result, second_result = one.result(timeout=15), two.result(timeout=15)
        assert (first_result, second_result) == (
            ("issued", 1) if first_owner == "oidc" else (0, "issued")
        )
    with Session(engine) as verify:
        session = verify.query(AuthSession).one()
        assert session.status is (
            SessionStatus.revoked if first_owner == "oidc" else SessionStatus.active
        )
        local = verify.get(UserCredential, scenario.local_credential_id)
        assert local.credential_version == 2
        assert (
            verify.get(OidcMobileCeremony, ceremony).outcome
            == OidcCeremonyOutcome.completed.value
        )
