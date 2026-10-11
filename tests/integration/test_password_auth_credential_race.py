"""PostgreSQL proof that password login, MFA completion and password change are
version-bound to the credential (R-PR1 of the credential-standing race fix).

These tests need a real PostgreSQL server: they prove ordering and blocking
between concurrent transactions, the credential_version trigger, and migration
669. They run in CI's PostgreSQL integration shards (``make
test-integration-shard``) and are NOT runnable on a developer machine without
a disposable ``TEST_DATABASE_URL`` database. Never point them at a live one.

Pattern (see ``test_auth_session_refresh_concurrency.py``): real threads, a
``sessionmaker(bind=engine)`` instead of the rollback-wrapped ``db_session``,
a timeout on every wait, cleanup in ``finally``. Deterministic interleaving
uses no production test seam:

* "pause after verify": the password verifier is wrapped to block one named
  thread after a successful verification;
* "pause while holding the gate lock": ``stage_session_issue`` is wrapped to
  block one named thread before delegating (it runs AFTER the gate UPDATE, so
  the credential row is locked by that thread);
* "the other session is blocked" is proven from ``pg_stat_activity``
  (``wait_event_type = 'Lock'``), never by sleeping.
"""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pyotp
import pytest
from alembic.config import Config
from cryptography.fernet import Fernet
from fastapi import HTTPException
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from alembic import command
from app.models.audit import AuditEvent
from app.models.auth import (
    AuthProvider,
    MFAMethod,
    MFARecoveryCode,
    SessionStatus,
    UserCredential,
)
from app.models.auth import Session as AuthSession
from app.models.catalog import AccessCredential
from app.models.event_store import EventStore
from app.models.subscriber import Subscriber
from app.services import auth_flow as auth_flow_service
from app.services import credential_recovery
from app.services import password_authentication as pa
from app.services.auth_flow import AuthFlow, change_password, hash_password
from app.services.owner_commands import CommandContext
from app.services.subscriber import _default_reseller_id
from tests.test_auth_flow import _make_request, _system_user_with_credential

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
REVISION_668 = "668_sole_approver_adjudication_evidence"
REVISION_670 = "670_user_credential_version"
WAIT = 20
CORRECT = "secret"
RESET_TO = "Reset-by-owner1!"


# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _signing(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "race-test-secret")
    monkeypatch.setenv("TOTP_ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))


@pytest.fixture
def factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def account(factory):
    suffix = uuid.uuid4().hex[:10]
    email = f"race-{suffix}@example.test"
    with factory() as setup:
        subscriber = Subscriber(
            first_name="Race",
            last_name=suffix,
            email=email,
            reseller_id=_default_reseller_id(setup),
        )
        setup.add(subscriber)
        setup.flush()
        credential = UserCredential(
            subscriber_id=subscriber.id,
            provider=AuthProvider.local,
            username=email,
            password_hash=hash_password("secret"),
            is_active=True,
        )
        setup.add(credential)
        setup.commit()
        data = SimpleNamespace(
            subscriber_id=subscriber.id,
            credential_id=credential.id,
            username=email,
            email=email,
        )
    try:
        yield data
    finally:
        # Best effort: audit rows are append-only evidence and are left behind
        # (they carry no foreign keys); a throwaway CI database absorbs them.
        with factory() as cleanup:
            try:
                methods = select(MFAMethod.id).where(
                    MFAMethod.subscriber_id == data.subscriber_id
                )
                cleanup.execute(
                    delete(MFARecoveryCode).where(
                        MFARecoveryCode.mfa_method_id.in_(methods)
                    )
                )
                cleanup.execute(
                    delete(MFAMethod).where(
                        MFAMethod.subscriber_id == data.subscriber_id
                    )
                )
                cleanup.execute(
                    delete(AuthSession).where(
                        AuthSession.subscriber_id == data.subscriber_id
                    )
                )
                cleanup.execute(
                    delete(UserCredential).where(
                        UserCredential.subscriber_id == data.subscriber_id
                    )
                )
                cleanup.execute(
                    delete(EventStore).where(
                        EventStore.subscriber_id == data.subscriber_id
                    )
                )
                cleanup.execute(
                    delete(Subscriber).where(Subscriber.id == data.subscriber_id)
                )
                cleanup.commit()
            except Exception:
                cleanup.rollback()


class _Pause:
    def __init__(self) -> None:
        self.reached = threading.Event()
        self.resume = threading.Event()

    def hit(self) -> None:
        self.reached.set()
        assert self.resume.wait(WAIT), "pause was never released"


def _named(prefix: str) -> bool:
    return threading.current_thread().name.startswith(prefix)


def _pause_after_verify(monkeypatch, pause: _Pause, prefix: str) -> None:
    real = auth_flow_service.verify_user_password

    def spy(password, password_hash):
        ok = real(password, password_hash)
        if ok and _named(prefix):
            pause.hit()
        return ok

    monkeypatch.setattr(auth_flow_service, "verify_user_password", spy)


def _pause_after_service_verify(monkeypatch, pause: _Pause, prefix: str) -> None:
    """Like ``_pause_after_verify`` for the PPPoE secret verifier."""

    real = auth_flow_service.verify_service_secret

    def spy(password, secret_hash):
        ok = real(password, secret_hash)
        if ok and _named(prefix):
            pause.hit()
        return ok

    monkeypatch.setattr(auth_flow_service, "verify_service_secret", spy)


def _pause_inside_gate(monkeypatch, pause: _Pause, prefix: str) -> None:
    """Block one thread after its gate UPDATE (it holds the credential row)."""

    real = auth_flow_service.stage_session_issue

    def spy(*args, **kwargs):
        if _named(prefix):
            pause.hit()
        return real(*args, **kwargs)

    monkeypatch.setattr(auth_flow_service, "stage_session_issue", spy)


def _wait_until_a_session_is_blocked(engine, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    with engine.connect() as observer:
        while time.monotonic() < deadline:
            blocked = observer.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() "
                    "AND wait_event_type = 'Lock' AND pid <> pg_backend_pid()"
                )
            ).scalar_one()
            if blocked:
                return
            time.sleep(0.05)
    pytest.fail("no session ever blocked on a lock")


def _login(factory, account, password=CORRECT):
    with factory() as session:
        try:
            return (
                "ok",
                AuthFlow.login(
                    session, account.username, password, _make_request(), None
                ),
            )
        except HTTPException as exc:
            return ("http", exc.status_code, exc.headers)


def _reset(factory, account, new_password=RESET_TO):
    with factory() as session:
        capability = credential_recovery.issue_exact_reset_capability(
            session, principal_type="subscriber", principal_id=account.subscriber_id
        )
        assert capability is not None
        session.commit()
        token = capability.token
    with factory() as session:
        return credential_recovery.complete_password_reset(
            session,
            credential_recovery.CompletePasswordResetCommand(
                context=CommandContext.system(
                    actor="service:pytest-credential-race",
                    scope=credential_recovery.CREDENTIAL_RECOVERY_SCOPE,
                    reason="Race test reset",
                ),
                token=token,
                new_password=new_password,
            ),
        )


def _credential_row(factory, account):
    with factory() as session:
        return session.execute(
            select(
                UserCredential.credential_version,
                UserCredential.failed_login_attempts,
                UserCredential.locked_until,
                UserCredential.last_login_at,
                UserCredential.password_hash,
            ).where(UserCredential.id == account.credential_id)
        ).one()


def _session_count(factory, account) -> int:
    with factory() as session:
        return session.scalar(
            select(func.count())
            .select_from(AuthSession)
            .where(AuthSession.subscriber_id == account.subscriber_id)
        )


def _audit_count(factory, account, action) -> int:
    with factory() as session:
        return session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.action == action,
                AuditEvent.entity_id == str(account.subscriber_id),
            )
        )


def _enable_mfa(factory, account):
    with factory() as session:
        setup = AuthFlow.mfa_setup(session, str(account.subscriber_id), label="device")
        method = AuthFlow.mfa_confirm(
            session,
            str(setup["method_id"]),
            pyotp.TOTP(setup["secret"]).now(),
            str(account.subscriber_id),
        )
        codes = auth_flow_service.generate_mfa_recovery_codes(session, method)
    return setup["secret"], setup["method_id"], codes


def _password_step(factory, account):
    kind, result, *_ = _login(factory, account)
    assert kind == "ok" and result["mfa_required"] is True
    return result["mfa_token"]


def _mfa_verify(factory, token, code):
    with factory() as session:
        try:
            return ("ok", AuthFlow.mfa_verify(session, token, code, _make_request()))
        except HTTPException as exc:
            return ("http", exc.status_code, exc.detail)


# --------------------------------------------------------------------------
# T1, T2: login vs reset
# --------------------------------------------------------------------------


def test_t1_reset_between_verify_and_gate_mints_no_session(
    engine, factory, account, monkeypatch
):
    pause = _Pause()
    _pause_after_verify(monkeypatch, pause, "racer-a")
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="racer-a") as pool:
        future = pool.submit(_login, factory, account)
        assert pause.reached.wait(WAIT)
        _reset(factory, account)
        pause.resume.set()
        outcome = future.result(timeout=WAIT)

    assert outcome[:2] == ("http", 401)
    row = _credential_row(factory, account)
    assert row.credential_version == 2
    assert auth_flow_service.verify_user_password("Reset-by-owner1!", row.password_hash)
    assert row.last_login_at is None
    assert _session_count(factory, account) == 0
    assert _audit_count(factory, account, "auth.login_succeeded") == 0


def test_t2_session_committed_before_reset_is_revoked_by_reset(
    engine, factory, account, monkeypatch
):
    """A holds the credential row after its gate; the reset is proven blocked,
    A commits its session, then the reset revokes it. No deadlock (the reset
    must succeed, not die with 40P01)."""

    pause = _Pause()
    _pause_inside_gate(monkeypatch, pause, "racer-a")
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="racer-a") as pool_a:
        login_future = pool_a.submit(_login, factory, account)
        assert pause.reached.wait(WAIT)
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="racer-b") as pool_b:
            reset_future = pool_b.submit(_reset, factory, account)
            _wait_until_a_session_is_blocked(engine)
            assert not reset_future.done()
            pause.resume.set()
            outcome = reset_future.result(timeout=WAIT)
        login_outcome = login_future.result(timeout=WAIT)

    assert login_outcome[0] == "ok"
    assert outcome.sessions_revoked >= 1
    with factory() as session:
        statuses = session.scalars(
            select(AuthSession.status).where(
                AuthSession.subscriber_id == account.subscriber_id
            )
        ).all()
    assert statuses and all(status is SessionStatus.revoked for status in statuses)


# --------------------------------------------------------------------------
# T3-T6: MFA challenge standing
# --------------------------------------------------------------------------


def test_t3_reset_between_password_step_and_mfa_completion(engine, factory, account):
    secret, method_id, codes = _enable_mfa(factory, account)
    token = _password_step(factory, account)
    _reset(factory, account)

    outcome = _mfa_verify(factory, token, codes[0])

    assert outcome[:2] == ("http", 401)
    assert _session_count(factory, account) == 0
    with factory() as session:
        unused = session.scalar(
            select(func.count())
            .select_from(MFARecoveryCode)
            .where(
                MFARecoveryCode.mfa_method_id == method_id,
                MFARecoveryCode.is_active.is_(True),
                MFARecoveryCode.used_at.is_(None),
            )
        )
        assert unused == len(codes)
        assert session.get(MFAMethod, method_id).last_used_at is None


def test_t4_must_change_password_via_legacy_writer_voids_challenge(
    engine, factory, account
):
    """A writer that does not bump (the admin API setattr loop) is caught by
    the trigger, which bumps the version and so voids the challenge."""

    secret, _method_id, _codes = _enable_mfa(factory, account)
    token = _password_step(factory, account)
    with factory() as session:
        credential = session.get(UserCredential, account.credential_id)
        credential.must_change_password = True
        session.commit()
    assert _credential_row(factory, account).credential_version == 2

    outcome = _mfa_verify(factory, token, pyotp.TOTP(secret).now())

    assert outcome[:2] == ("http", 401)
    assert _session_count(factory, account) == 0


def test_t5_deactivation_voids_challenge(engine, factory, account):
    secret, _method_id, _codes = _enable_mfa(factory, account)
    token = _password_step(factory, account)
    with factory() as session:
        session.get(UserCredential, account.credential_id).is_active = False
        session.commit()

    outcome = _mfa_verify(factory, token, pyotp.TOTP(secret).now())

    assert outcome[:2] == ("http", 401)
    assert _session_count(factory, account) == 0


def test_t6_representation_only_rehash_does_not_void_challenge(
    engine, factory, account
):
    secret, _method_id, _codes = _enable_mfa(factory, account)
    token = _password_step(factory, account)
    before = _credential_row(factory, account)
    with factory() as session:
        assert pa.replace_password_representation(
            session,
            credential_id=account.credential_id,
            expected_version=before.credential_version,
            expected_hash=before.password_hash,
            new_hash=hash_password("secret"),
        )
        session.commit()
    after = _credential_row(factory, account)
    assert after.credential_version == before.credential_version
    assert after.password_hash != before.password_hash

    outcome = _mfa_verify(factory, token, pyotp.TOTP(secret).now())

    assert outcome[0] == "ok"
    assert _session_count(factory, account) == 1


# --------------------------------------------------------------------------
# T7-T9: concurrency without lost updates
# --------------------------------------------------------------------------


def _parallel(count, work):
    barrier = threading.Barrier(count)

    def run(index):
        barrier.wait(timeout=WAIT)
        return work(index)

    with ThreadPoolExecutor(max_workers=count) as pool:
        futures = [pool.submit(run, index) for index in range(count)]
        return [future.result(timeout=WAIT * 2) for future in futures]


def test_t7_two_concurrent_correct_logins_both_succeed(engine, factory, account):
    results = _parallel(2, lambda _i: _login(factory, account))

    assert [result[0] for result in results] == ["ok", "ok"]
    assert _session_count(factory, account) == 2
    assert _credential_row(factory, account).credential_version == 1


def test_t8_parallel_wrong_passwords_equal_the_sequential_lock_result(
    engine, factory, account
):
    attempts = auth_flow_service.LOGIN_MAX_FAILED_ATTEMPTS + 3

    results = _parallel(attempts, lambda _i: _login(factory, account, "wrong"))

    assert {result[1] for result in results} <= {401, 403}
    row = _credential_row(factory, account)
    assert row.failed_login_attempts == auth_flow_service.LOGIN_MAX_FAILED_ATTEMPTS
    assert row.locked_until is not None
    assert _login(factory, account, "secret")[:2] == ("http", 403)


def test_t9_one_recovery_code_cannot_be_spent_twice(engine, factory, account):
    _secret, _method_id, codes = _enable_mfa(factory, account)
    tokens = [_password_step(factory, account), _password_step(factory, account)]

    results = _parallel(2, lambda index: _mfa_verify(factory, tokens[index], codes[0]))

    assert sorted(result[0] for result in results) == ["http", "ok"]
    assert _session_count(factory, account) == 1
    with factory() as session:
        spent = session.scalar(
            select(func.count())
            .select_from(MFARecoveryCode)
            .where(MFARecoveryCode.used_at.is_not(None))
        )
        assert spent >= 1
        used_for_account = session.scalar(
            select(func.count())
            .select_from(MFARecoveryCode)
            .join(MFAMethod, MFAMethod.id == MFARecoveryCode.mfa_method_id)
            .where(
                MFAMethod.subscriber_id == account.subscriber_id,
                MFARecoveryCode.used_at.is_not(None),
            )
        )
        assert used_for_account == 1


# --------------------------------------------------------------------------
# T10: password change vs reset
# --------------------------------------------------------------------------


def test_t10_change_password_never_overwrites_a_concurrent_reset(
    engine, factory, account, monkeypatch
):
    pause = _Pause()
    _pause_after_verify(monkeypatch, pause, "racer-a")

    def change():
        with factory() as session:
            try:
                change_password(
                    session, str(account.subscriber_id), "secret", "Brandnew1!"
                )
                return ("ok",)
            except HTTPException as exc:
                return ("http", exc.status_code)

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="racer-a") as pool:
        future = pool.submit(change)
        assert pause.reached.wait(WAIT)
        _reset(factory, account)
        pause.resume.set()
        outcome = future.result(timeout=WAIT)

    assert outcome == ("http", 409)
    row = _credential_row(factory, account)
    assert auth_flow_service.verify_user_password("Reset-by-owner1!", row.password_hash)
    assert row.credential_version == 2


# --------------------------------------------------------------------------
# T11: trigger matrix
# --------------------------------------------------------------------------


def _version(engine, credential_id) -> int:
    with engine.connect() as connection:
        return connection.execute(
            select(UserCredential.credential_version).where(
                UserCredential.id == credential_id
            )
        ).scalar_one()


def _run_update(engine, credential_id, **values) -> int:
    with engine.begin() as connection:
        connection.execute(
            update(UserCredential)
            .where(UserCredential.id == credential_id)
            .values(**values)
        )
    return _version(engine, credential_id)


def test_t11_trigger_matrix(engine, factory, account):
    cid = account.credential_id
    assert _version(engine, cid) == 1

    # Not standing: no bump.
    assert _run_update(engine, cid, failed_login_attempts=3) == 1
    assert _run_update(engine, cid, last_login_at=func.now()) == 1
    assert _run_update(engine, cid, locked_until=func.now()) == 1
    assert _run_update(engine, cid, updated_at=func.now()) == 1

    # Standing: auto-bump for a writer that did not bump.
    assert _run_update(engine, cid, password_hash=hash_password("other")) == 2
    assert _run_update(engine, cid, is_active=False) == 3
    assert _run_update(engine, cid, is_active=True) == 4
    assert _run_update(engine, cid, must_change_password=True) == 5
    assert (
        _run_update(engine, cid, must_change_password=False) == 5
    )  # clearing: no bump
    assert _run_update(engine, cid, username=f"renamed-{uuid.uuid4().hex[:8]}") == 6

    # An explicit bump is honoured, not doubled.
    assert (
        _run_update(
            engine,
            cid,
            password_hash=hash_password("third"),
            credential_version=UserCredential.credential_version + 1,
        )
        == 7
    )

    # The representation switch is scoped to exactly one statement.
    with factory() as session:
        row = session.execute(
            select(
                UserCredential.password_hash, UserCredential.credential_version
            ).where(UserCredential.id == cid)
        ).one()
        assert pa.replace_password_representation(
            session,
            credential_id=cid,
            expected_version=row.credential_version,
            expected_hash=row.password_hash,
            new_hash=hash_password("third"),
        )
        # Same transaction, next hash change: the switch is already cleared.
        session.execute(
            update(UserCredential)
            .where(UserCredential.id == cid)
            .values(password_hash=hash_password("fourth"))
        )
        session.commit()
    assert _version(engine, cid) == 8

    # A decrease is refused (check_violation from the trigger / CHECK).
    with pytest.raises(IntegrityError):
        _run_update(engine, cid, credential_version=1)
    with pytest.raises(IntegrityError):
        _run_update(engine, cid, credential_version=0)
    assert _version(engine, cid) == 8


# --------------------------------------------------------------------------
# T12: lock timeout
# --------------------------------------------------------------------------


def test_t12_busy_credential_row_yields_503_and_changes_nothing(
    engine, factory, account
):
    holder = engine.connect()
    transaction = holder.begin()
    try:
        holder.execute(
            text("SELECT 1 FROM user_credentials WHERE id = :id FOR UPDATE"),
            {"id": account.credential_id},
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_login, factory, account)
            outcome = future.result(timeout=40)
    finally:
        transaction.rollback()
        holder.close()

    assert outcome[:2] == ("http", 503)
    assert outcome[2]["Retry-After"] == "5"
    row = _credential_row(factory, account)
    assert row.failed_login_attempts == 0
    assert row.last_login_at is None
    assert _session_count(factory, account) == 0


# --------------------------------------------------------------------------
# T14, T15: atomicity of the transition
# --------------------------------------------------------------------------


def test_t14_audit_failure_inside_the_transition_fails_closed(
    engine, factory, account, monkeypatch
):
    def boom(*_args, **_kwargs):
        raise RuntimeError("audit store unavailable")

    monkeypatch.setattr(pa, "stage_audit_event", boom)

    with factory() as session:
        with pytest.raises(RuntimeError):
            AuthFlow.login(session, account.username, "secret", _make_request(), None)

    row = _credential_row(factory, account)
    assert row.last_login_at is None
    assert _session_count(factory, account) == 0


def test_t15_staff_login_session_presence_and_rollback(engine, factory, monkeypatch):
    from app.models.team_inbox import InboxAgentPresence
    from app.services import team_inbox_assignment

    email = f"staff-race-{uuid.uuid4().hex[:8]}@example.test"
    with factory() as setup:
        system_user, credential = _system_user_with_credential(setup, email)
        system_user_id, credential_id = system_user.id, credential.id

    with factory() as session:
        result = AuthFlow.login(session, email, "secret", _make_request(), None)
    assert result["access_token"]
    with factory() as session:
        auth_session = session.scalar(
            select(AuthSession).where(AuthSession.system_user_id == system_user_id)
        )
        assert auth_session is not None and auth_session.party_id is not None
        presence = session.scalar(
            select(func.count())
            .select_from(InboxAgentPresence)
            .where(InboxAgentPresence.person_id == system_user_id)
        )
        assert presence >= 1
        before = session.execute(
            select(UserCredential.last_login_at).where(
                UserCredential.id == credential_id
            )
        ).scalar_one()

    def boom(*_args, **_kwargs):
        raise RuntimeError("presence unavailable")

    monkeypatch.setattr(team_inbox_assignment, "record_agent_signed_in_presence", boom)
    with factory() as session:
        with pytest.raises(RuntimeError):
            AuthFlow.login(session, email, "secret", _make_request(), None)
    with factory() as session:
        after = session.execute(
            select(UserCredential.last_login_at).where(
                UserCredential.id == credential_id
            )
        ).scalar_one()
        assert after == before  # the gate rolled back with the failed presence write
        count = session.scalar(
            select(func.count())
            .select_from(AuthSession)
            .where(AuthSession.system_user_id == system_user_id)
        )
        assert count == 1


# --------------------------------------------------------------------------
# T13: migration 670 on a clone of the 668 schema
# --------------------------------------------------------------------------


def _render(url) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def _alembic(direction: str, revision: str) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    getattr(command, direction)(config, revision)


def test_t13_migration_670_upgrade_and_downgrade_on_a_668_clone(
    engine, cloned_database
):
    url = cloned_database(REVISION_668)
    ids = [uuid.uuid4() for _ in range(3)]
    with psycopg.connect(_render(url), autocommit=True) as connection:
        # Foreign keys are out of scope for this proof: replica role skips the
        # RI triggers so rows can exist without building a subscriber graph.
        connection.execute("SET session_replication_role = replica")
        for index, credential_id in enumerate(ids):
            connection.execute(
                "INSERT INTO user_credentials (id, subscriber_id, provider, "
                "username, password_hash, must_change_password, "
                "failed_login_attempts, is_active, created_at, updated_at) "
                "VALUES (%s, %s, 'local', %s, 'hash', false, 0, true, now(), now())",
                (credential_id, uuid.uuid4(), f"migrate-{index}-{uuid.uuid4().hex}"),
            )

    _alembic("upgrade", REVISION_670)

    with psycopg.connect(_render(url), autocommit=True) as connection:
        versions = connection.execute(
            "SELECT credential_version FROM user_credentials"
        ).fetchall()
        assert versions == [(1,)] * 3
        nullable = connection.execute(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = 'user_credentials' "
            "AND column_name = 'credential_version'"
        ).fetchone()
        assert nullable == ("NO",)
        triggers = connection.execute(
            "SELECT tgname FROM pg_trigger WHERE tgrelid = "
            "'user_credentials'::regclass AND NOT tgisinternal"
        ).fetchall()
        assert ("trg_user_credentials_credential_version",) in triggers
        validated = connection.execute(
            "SELECT convalidated FROM pg_constraint WHERE conname = "
            "'ck_user_credentials_credential_version_positive'"
        ).fetchone()
        assert validated == (True,)

    _alembic("downgrade", REVISION_668)

    with psycopg.connect(_render(url), autocommit=True) as connection:
        assert connection.execute(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = 'user_credentials' "
            "AND column_name = 'credential_version'"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM pg_trigger WHERE tgname = "
            "'trg_user_credentials_credential_version'"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM pg_proc WHERE proname = "
            "'user_credentials_credential_version_guard'"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM user_credentials"
        ).fetchone() == (3,)


# --------------------------------------------------------------------------
# R-PR1b: forced MFA enrollment bound to credential standing; PPPoE row lock
# --------------------------------------------------------------------------


def _staff_enrollment(factory, monkeypatch):
    """Staff user whose password step demands enrollment, with a pending TOTP."""

    monkeypatch.setattr(auth_flow_service, "_force_admin_mfa", lambda _db: True)
    email = f"staff-enroll-{uuid.uuid4().hex[:8]}@example.test"
    with factory() as setup:
        system_user, credential = _system_user_with_credential(setup, email)
        staff = SimpleNamespace(
            id=system_user.id, credential_id=credential.id, email=email
        )
    with factory() as session:
        step = AuthFlow.login(session, email, "secret", _make_request(), None)
    assert step["mfa_enrollment_required"] is True
    with factory() as session:
        setup_data = AuthFlow.admin_mfa_setup(session, str(staff.id), "device")
    return staff, step["mfa_enrollment_token"], setup_data


def _reset_staff(factory, staff):
    with factory() as session:
        session.execute(
            update(UserCredential)
            .where(UserCredential.id == staff.credential_id)
            .values(
                password_hash=hash_password(RESET_TO),
                credential_version=UserCredential.credential_version + 1,
            )
        )
        session.commit()


def _enrollment_state(factory, staff, method_id):
    with factory() as session:
        enabled = session.scalar(
            select(func.count())
            .select_from(MFAMethod)
            .where(MFAMethod.system_user_id == staff.id, MFAMethod.enabled.is_(True))
        )
        codes = session.scalar(
            select(func.count())
            .select_from(MFARecoveryCode)
            .where(MFARecoveryCode.mfa_method_id == method_id)
        )
        sessions = session.scalar(
            select(func.count())
            .select_from(AuthSession)
            .where(AuthSession.system_user_id == staff.id)
        )
    return enabled, codes, sessions


def _confirm(factory, token, setup_data, code=None):
    with factory() as session:
        try:
            return (
                "ok",
                AuthFlow.confirm_mfa_enrollment(
                    session,
                    token,
                    str(setup_data["method_id"]),
                    code or pyotp.TOTP(setup_data["secret"]).now(),
                    _make_request(),
                ),
            )
        except HTTPException as exc:
            return ("http", exc.status_code, exc.detail)


def test_enrollment_with_matching_version_commits_factor_codes_and_session(
    engine, factory, monkeypatch
):
    staff, token, setup_data = _staff_enrollment(factory, monkeypatch)
    outcome = _confirm(factory, token, setup_data)
    assert outcome[0] == "ok"
    enabled, codes, sessions = _enrollment_state(
        factory, staff, setup_data["method_id"]
    )
    assert (enabled, sessions) == (1, 1)
    assert codes == len(outcome[1][1]) > 0


def test_stale_enrollment_token_after_reset_persists_no_factor_codes_or_session(
    engine, factory, monkeypatch
):
    staff, token, setup_data = _staff_enrollment(factory, monkeypatch)
    _reset_staff(factory, staff)
    outcome = _confirm(factory, token, setup_data)
    assert outcome[:2] == ("http", 401)
    assert _enrollment_state(factory, staff, setup_data["method_id"]) == (0, 0, 0)


def test_reset_between_enrollment_verify_and_gate_rolls_everything_back(
    engine, factory, monkeypatch
):
    staff, token, setup_data = _staff_enrollment(factory, monkeypatch)
    pause = _Pause()
    real = pa.confirm_mfa_enrollment

    def spy(*args, **kwargs):
        if _named("racer-a"):
            pause.hit()
        return real(*args, **kwargs)

    monkeypatch.setattr(pa, "confirm_mfa_enrollment", spy)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="racer-a") as pool:
        future = pool.submit(_confirm, factory, token, setup_data)
        assert pause.reached.wait(WAIT)
        _reset_staff(factory, staff)
        pause.resume.set()
        outcome = future.result(timeout=WAIT)
    assert outcome[:2] == ("http", 401)
    assert _enrollment_state(factory, staff, setup_data["method_id"]) == (0, 0, 0)


def test_legacy_enrollment_token_is_refused(engine, factory, monkeypatch):
    staff, _token, setup_data = _staff_enrollment(factory, monkeypatch)
    payload = {
        "sub": str(staff.id),
        "principal_id": str(staff.id),
        "principal_type": "system_user",
        "typ": "mfa_enrollment",
        "exp": int(time.time()) + 300,
    }
    with factory() as session:
        token = auth_flow_service._jwt_encode_token(  # noqa: SLF001
            payload,
            auth_flow_service._jwt_secret(session),  # noqa: SLF001
            auth_flow_service._jwt_algorithm(session),  # noqa: SLF001
        )
    outcome = _confirm(factory, token, setup_data)
    assert outcome[:2] == ("http", 401)
    assert _enrollment_state(factory, staff, setup_data["method_id"]) == (0, 0, 0)


@pytest.fixture
def pppoe(factory, account):
    with factory() as setup:
        access = AccessCredential(
            subscriber_id=account.subscriber_id,
            username=f"pppoe-race-{uuid.uuid4().hex[:8]}",
            secret_hash=auth_flow_service.hash_service_secret("pppoe-secret"),
            is_active=True,
        )
        setup.add(access)
        setup.commit()
        data = SimpleNamespace(id=access.id, username=access.username)
    try:
        yield data
    finally:
        with factory() as cleanup:
            try:
                cleanup.execute(
                    delete(AccessCredential).where(AccessCredential.id == data.id)
                )
                cleanup.commit()
            except Exception:
                cleanup.rollback()


def _rotate_pppoe_secret(factory, pppoe):
    with factory() as session:
        session.execute(
            update(AccessCredential)
            .where(AccessCredential.id == pppoe.id)
            .values(secret_hash=auth_flow_service.hash_service_secret("rotated"))
        )
        session.commit()


def _pppoe_login(factory, pppoe):
    with factory() as session:
        try:
            return (
                "ok",
                AuthFlow.login(
                    session, pppoe.username, "pppoe-secret", _make_request(), None
                ),
            )
        except HTTPException as exc:
            return ("http", exc.status_code)


def test_pppoe_secret_change_before_the_gate_mints_no_session(
    engine, factory, account, pppoe, monkeypatch
):
    pause = _Pause()
    _pause_after_service_verify(monkeypatch, pause, "racer-a")
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="racer-a") as pool:
        future = pool.submit(_pppoe_login, factory, pppoe)
        assert pause.reached.wait(WAIT)
        _rotate_pppoe_secret(factory, pppoe)
        pause.resume.set()
        outcome = future.result(timeout=WAIT)
    assert outcome[:2] == ("http", 401)
    assert _session_count(factory, account) == 0


def test_pppoe_secret_change_cannot_commit_while_the_gate_holds_its_row(
    engine, factory, account, pppoe, monkeypatch
):
    """The gate holds the access-credential row FOR SHARE until the session
    commits: a rotation is blocked (proved from pg_stat_activity), then lands
    after the login, never in the middle of it."""

    pause = _Pause()
    _pause_inside_gate(monkeypatch, pause, "racer-a")
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="racer-a") as pool_a:
        login_future = pool_a.submit(_pppoe_login, factory, pppoe)
        assert pause.reached.wait(WAIT)
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="racer-b") as pool_b:
            rotate_future = pool_b.submit(_rotate_pppoe_secret, factory, pppoe)
            _wait_until_a_session_is_blocked(engine)
            assert not rotate_future.done()
            pause.resume.set()
            rotate_future.result(timeout=WAIT)
        outcome = login_future.result(timeout=WAIT)
    assert outcome[0] == "ok"
    assert _session_count(factory, account) == 1


def test_enrollment_token_replayed_after_victim_enrolled_is_refused(
    engine, factory, monkeypatch
):
    staff, token, setup_data = _staff_enrollment(factory, monkeypatch)
    assert _confirm(factory, token, setup_data)[0] == "ok"
    with factory() as session:
        attacker = AuthFlow.admin_mfa_setup(session, str(staff.id), "evil")
    outcome = _confirm(factory, token, attacker)
    assert outcome[:2] == ("http", 401)
    with factory() as session:
        primary = session.scalars(
            select(MFAMethod.id).where(
                MFAMethod.system_user_id == staff.id, MFAMethod.is_primary.is_(True)
            )
        ).all()
    assert primary == [setup_data["method_id"]]
    assert _session_count_staff(factory, staff) == 1


def _session_count_staff(factory, staff) -> int:
    with factory() as session:
        return session.scalar(
            select(func.count())
            .select_from(AuthSession)
            .where(AuthSession.system_user_id == staff.id)
        )


def test_reenrollment_after_admin_mfa_reset_leaves_exactly_one_primary(
    engine, factory, monkeypatch
):
    from app.services.web_system_user_mutations import disable_user_mfa

    staff, token, setup_data = _staff_enrollment(factory, monkeypatch)
    assert _confirm(factory, token, setup_data)[0] == "ok"
    with factory() as session:
        from tests.test_password_authentication import _remove_mfa_command

        user_id = staff.id
        session.commit()
        disable_user_mfa(session, command=_remove_mfa_command(user_id))
        step = AuthFlow.login(session, staff.email, "secret", _make_request(), None)
        assert step["mfa_enrollment_required"] is True
        new_setup = AuthFlow.admin_mfa_setup(session, str(staff.id), "device2")
    outcome = _confirm(factory, step["mfa_enrollment_token"], new_setup)
    assert outcome[0] == "ok"
    with factory() as session:
        primary = session.scalars(
            select(MFAMethod.id).where(
                MFAMethod.system_user_id == staff.id, MFAMethod.is_primary.is_(True)
            )
        ).all()
    assert primary == [new_setup["method_id"]]
