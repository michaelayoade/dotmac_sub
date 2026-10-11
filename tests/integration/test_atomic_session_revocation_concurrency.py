"""Hosted-only exact lock graph proofs on real Alembic heads clones.

Locally collect only. Reuse the reviewed OIDC clone/scenario and blocking-pid
probe; no metadata schema or timing-based inference substitutes for PostgreSQL.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.models.auth import Session as AuthSession
from app.models.auth import SessionStatus
from app.models.system_user import SystemUser
from app.services import credential_recovery
from app.services import session_manager as sm
from app.services.audit_adapter import AuditActor
from app.services.auth_flow import AuthFlow
from app.services.auth_session_refresh import (
    RefreshSessionCommand,
    hash_refresh_token,
    renew_authentication_session,
)
from app.services.credential_party_binding import (
    AUTHENTICATION_BINDING_INSTALL_SCOPE,
    AuthenticationBindingInstallation,
    CredentialPartyBinding,
    CredentialPrincipalKind,
    bind_credential_party,
    install_authentication_binding,
)
from app.services.db_session_adapter import db_session_adapter
from app.services.operator_tenant import OPERATOR_TENANT_ID
from app.services.owner_commands import CommandContext
from tests.integration import test_oidc_atomic_session_concurrency as race_fixtures
from tests.integration.test_oidc_atomic_session_concurrency import (
    Scenario,
    Worker,
    _blocked_by,
    _exchange_worker,
    _start_assertion,
)
from tests.oidc_mobile_fixtures import CREDENTIAL_BINDING_SCOPE
from tests.test_auth_flow import _make_request

engine = race_fixtures.engine
scenario = race_fixtures.scenario


def _context(actor: str = "test:revocation") -> CommandContext:
    return CommandContext.system(
        actor=actor,
        scope="session_revocation_concurrency",
        reason="prove principal-first lock ordering",
    )


@pytest.fixture
def cohort(engine: Engine, scenario: Scenario) -> tuple[UUID, UUID]:
    with Session(engine) as db:
        principal = db.get(SystemUser, scenario.principal_id)
        party_id = principal.person_party_id
        db.rollback()
        installed = install_authentication_binding(
            db,
            AuthenticationBindingInstallation(
                context=CommandContext.system(
                    actor="test:revocation-operator",
                    scope=AUTHENTICATION_BINDING_INSTALL_SCOPE,
                    reason="install reviewed local fixture binding",
                ),
                binding_key=f"local.revocation.{uuid4()}",
                mechanism_code="local",
                name="Revocation local password",
            ),
        )
        bind_credential_party(
            db,
            CredentialPartyBinding(
                context=CommandContext.system(
                    actor="test:revocation-operator",
                    scope=CREDENTIAL_BINDING_SCOPE,
                    reason="project reviewed local fixture credential",
                ),
                credential_id=scenario.local_credential_id,
                expected_principal_kind=CredentialPrincipalKind.system_user,
                expected_principal_id=scenario.principal_id,
                party_id=party_id,
                authentication_binding_id=installed.binding_id,
                tenant_id=OPERATOR_TENANT_ID,
                binding_source="test-fixture",
                binding_reason="Reviewed session revocation fixture",
            ),
        )
        principal = db.get(SystemUser, scenario.principal_id)
        rows = [
            AuthSession(
                id=uuid4(),
                system_user_id=principal.id,
                party_id=principal.person_party_id,
                token_hash=hash_refresh_token(token),
                status=SessionStatus.active,
                expires_at=datetime.now(UTC) + timedelta(days=1),
                ip_address="127.0.0.1",
                user_agent="test",
            )
            for token in ("current-race-token", "target-race-token")
        ]
        db.add_all(rows)
        db.flush()
        ids = rows[0].id, rows[1].id
        db.commit()
        return ids


def _revoke_worker(
    engine: Engine,
    scenario: Scenario,
    cohort: tuple[UUID, UUID],
    worker: Worker,
    *,
    others: bool = False,
    pause: bool = False,
    logout: bool = False,
):
    with engine.connect() as connection, Session(connection) as db:
        worker.identify(db)
        if pause:
            event.listen(db, "before_commit", worker.pause_before_commit)
        else:
            worker.ready.set()
        return sm.revoke_authentication_sessions(
            db,
            sm.RevokeSessionsCommand(
                context=_context(str(scenario.principal_id)),
                intent=sm.RevocationIntent.LOGOUT
                if logout
                else sm.RevocationIntent.OTHERS
                if others
                else sm.RevocationIntent.ADMIN,
                actor=AuditActor.user(str(scenario.principal_id)),
                principal_id=scenario.principal_id,
                principal_type=sm.SessionPrincipalType.SYSTEM_USER,
                current_session_id=cohort[0],
                session_id=cohort[1],
                refresh_token="target-race-token" if logout else None,
            ),
        )


def _refresh_worker(engine: Engine, worker: Worker, *, pause: bool = False):
    with engine.connect() as connection, Session(connection) as db:
        worker.identify(db)
        if pause:
            paused = False

            def after_session_lock(
                conn, cursor, statement, parameters, context, executemany
            ):
                nonlocal paused
                if (
                    not paused
                    and "from sessions" in statement.lower()
                    and "for update" in statement.lower()
                ):
                    paused = True
                    worker.ready.set()
                    assert worker.release.wait(8), (
                        "test did not release the refresh lock"
                    )

            event.listen(connection, "after_cursor_execute", after_session_lock)
        else:
            worker.ready.set()
        from app.services.auth_session_refresh import RefreshSessionError

        try:
            renew_authentication_session(
                db,
                RefreshSessionCommand(
                    context=_context(),
                    refresh_token="target-race-token",
                    client_ip="127.0.0.1",
                    user_agent="test",
                ),
            )
        except RefreshSessionError:
            return "refused"
        return "issued"


def _reset_worker(engine: Engine, scenario: Scenario, worker: Worker):
    with engine.connect() as connection, Session(connection) as db:
        worker.identify(db)
        worker.ready.set()
        return credential_recovery.complete_password_reset(
            db,
            credential_recovery.CompletePasswordResetCommand(
                context=CommandContext.system(
                    actor="system:session_revocation_test",
                    scope=credential_recovery.CREDENTIAL_RECOVERY_SCOPE,
                    reason="prove reset serializes after revocation",
                ),
                token=scenario.reset_token,
                new_password="New-Race-Password-44!",
            ),
        )


def _login_worker(engine: Engine, scenario: Scenario, worker: Worker):
    with engine.connect() as connection, Session(connection) as db:
        worker.identify(db)
        email = db.scalar(
            select(SystemUser.email).where(SystemUser.id == scenario.principal_id)
        )
        db.rollback()
        worker.ready.set()
        return AuthFlow.login(
            db,
            username=email,
            password="Original-Atomic-Password-44!",
            request=_make_request(),
            provider="local",
        )


@pytest.mark.parametrize("other", ["refresh", "revoke", "oidc", "reset", "issuance"])
def test_committed_revocation_serializes_concurrent_transition(
    engine: Engine,
    scenario: Scenario,
    cohort: tuple[UUID, UUID],
    other: str,
    monkeypatch,
):

    @contextmanager
    def fresh():
        with Session(engine) as db:
            yield db

    monkeypatch.setattr(db_session_adapter, "owner_command_session", fresh)
    blocker, waiter = Worker(), Worker()
    ceremony, assertion = _start_assertion(engine, scenario)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            _revoke_worker,
            engine,
            scenario,
            cohort,
            blocker,
            others=other in {"oidc", "reset", "issuance"},
            pause=True,
            logout=other == "refresh",
        )
        assert blocker.ready.wait(3) and blocker.backend_pid
        if other == "refresh":
            second = pool.submit(_refresh_worker, engine, waiter)
        elif other == "revoke":
            second = pool.submit(_revoke_worker, engine, scenario, cohort, waiter)
        elif other == "oidc":
            second = pool.submit(_exchange_worker, engine, ceremony, assertion, waiter)
        elif other == "reset":
            second = pool.submit(_reset_worker, engine, scenario, waiter)
        else:
            second = pool.submit(_login_worker, engine, scenario, waiter)
        try:
            _blocked_by(engine, waiter=waiter, blocker=blocker.backend_pid)
        finally:
            blocker.release.set()
        first_result = first.result(timeout=15)
        second_result = second.result(timeout=15)
    assert first_result.revoked_ids == (cohort[1],)
    if other == "refresh":
        assert second_result == "refused"
    elif other == "revoke":
        assert second_result.revoked_ids == ()
    elif other == "oidc":
        assert second_result == "issued"
    with Session(engine) as verify:
        assert verify.get(AuthSession, cohort[1]).status is SessionStatus.revoked
        if other in {"oidc", "issuance"}:
            # A login serialized AFTER signout-others is a later legitimate login.
            assert (
                verify.scalar(
                    select(AuthSession.id).where(
                        AuthSession.system_user_id == scenario.principal_id,
                        AuthSession.id.notin_(cohort),
                        AuthSession.status == SessionStatus.active,
                    )
                )
                is not None
            )


def _logout_worker(
    engine: Engine, scenario: Scenario, cohort: tuple[UUID, UUID], worker: Worker
):
    try:
        _revoke_worker(engine, scenario, cohort, worker, logout=True)
    except sm.SessionRevocationError as exc:
        assert exc.code == "app_sessions.auth.not_found"
        return "not_found"
    return "revoked"


def test_refresh_first_logout_waits_without_principal_session_cycle(
    engine: Engine, scenario: Scenario, cohort: tuple[UUID, UUID], monkeypatch
):
    refreshing, logout = Worker(), Worker()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_refresh_worker, engine, refreshing, pause=True)
        assert refreshing.ready.wait(3) and refreshing.backend_pid
        second = pool.submit(_logout_worker, engine, scenario, cohort, logout)
        try:
            # Refresh is paused immediately after its initial session row lock,
            # before downstream staff resolution, signing, flush or commit.
            # Logout owns principal UPDATE and waits for this session UPDATE.
            # Resuming refresh must not acquire principal/Party in reverse order.
            _blocked_by(engine, waiter=logout, blocker=refreshing.backend_pid)
        finally:
            refreshing.release.set()
        assert first.result(timeout=15) == "issued"
        # Existing current-token-only semantics: the rotated old bearer no
        # longer authorizes logout, even though discovery preceded the lock.
        assert second.result(timeout=15) == "not_found"
    with Session(engine) as verify:
        row = verify.get(AuthSession, cohort[1])
        assert row.status is SessionStatus.active and row.revoked_at is None
        assert row.token_hash != hash_refresh_token("target-race-token")
