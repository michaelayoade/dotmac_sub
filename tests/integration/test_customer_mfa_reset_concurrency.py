"""Hosted migrated-PostgreSQL proof; collect-only on this workstation.

Composed setup/confirm cases require the subscriber enrollment owner slice.
Password phase-A snapshots are synthetic: these tests exercise phase-B database
standing and real principal foreign-key lock ordering without native hashing.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from queue import Queue
from threading import Event, current_thread
from uuid import UUID, uuid4

import pyotp
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import delete, select, text
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from app.models.audit import AuditEvent
from app.models.auth import (
    AuthProvider,
    MFAMethod,
    MFAMethodType,
    MFARecoveryCode,
    SessionStatus,
    UserCredential,
)
from app.models.auth import Session as AuthSession
from app.models.event_store import EventStore
from app.models.subscriber import Subscriber
from app.models.system_user import SystemUser
from app.services import auth_flow as af
from app.services import credential_administration as owner
from app.services import password_authentication as pa
from app.services.owner_commands import CommandContext
from app.services.subscriber import _default_reseller_id
from tests.integration.test_credential_administration_concurrency import (
    wait_for_exact_lock,
)

pytestmark = pytest.mark.integration
WAIT = 15
SECRET = "JBSWY3DPEHPK3PXP"


@dataclass(frozen=True)
class Account:
    subscriber_id: UUID
    actor_id: UUID
    credential_id: UUID
    method_id: UUID
    code_id: UUID
    session_id: UUID
    code_hash: str


@pytest.fixture
def factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def account(factory, monkeypatch):
    monkeypatch.setenv("TOTP_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("JWT_SECRET", "synthetic-customer-mfa-reset-signing")
    with factory() as db:
        suffix = uuid4().hex
        subscriber = Subscriber(
            first_name="MFA",
            last_name="Reset",
            email=f"reset-{suffix}@example.test",
            reseller_id=_default_reseller_id(db),
        )
        actor = SystemUser(
            first_name="Support",
            last_name="Reset",
            email=f"staff-{suffix}@example.test",
        )
        db.add_all([subscriber, actor])
        db.flush()
        credential = UserCredential(
            subscriber_id=subscriber.id,
            provider=AuthProvider.local,
            username=f"reset-{suffix}",
            password_hash="synthetic-phase-a-preverified",
            is_active=True,
        )
        method = MFAMethod(
            subscriber_id=subscriber.id,
            method_type=MFAMethodType.totp,
            enabled=True,
            is_active=True,
            is_primary=True,
            secret=af._encrypt_secret(db, SECRET),
        )
        session = AuthSession(
            subscriber_id=subscriber.id,
            token_hash=f"synthetic-{suffix}",
            expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        )
        db.add_all([credential, method, session])
        db.flush()
        code = MFARecoveryCode(
            mfa_method_id=method.id, code_hash=f"synthetic-code-{suffix}"
        )
        db.add(code)
        db.flush()
        data = Account(
            subscriber.id,
            actor.id,
            credential.id,
            method.id,
            code.id,
            session.id,
            code.code_hash,
        )
        db.commit()
    try:
        yield data
    finally:
        with factory() as db:
            methods = select(MFAMethod.id).where(
                MFAMethod.subscriber_id == data.subscriber_id
            )
            db.execute(
                delete(MFARecoveryCode).where(
                    MFARecoveryCode.mfa_method_id.in_(methods)
                )
            )
            db.execute(
                delete(MFAMethod).where(MFAMethod.subscriber_id == data.subscriber_id)
            )
            db.execute(
                delete(AuthSession).where(
                    AuthSession.subscriber_id == data.subscriber_id
                )
            )
            db.execute(
                delete(UserCredential).where(
                    UserCredential.subscriber_id == data.subscriber_id
                )
            )
            db.execute(
                delete(EventStore).where(EventStore.subscriber_id == data.subscriber_id)
            )
            db.execute(delete(Subscriber).where(Subscriber.id == data.subscriber_id))
            db.execute(delete(SystemUser).where(SystemUser.id == data.actor_id))
            db.commit()


def reset(factory, account):
    with factory() as db:
        return owner.reset_customer_mfa(
            db,
            command=owner.ResetCustomerMfaCommand(
                CommandContext.system(
                    actor=str(account.actor_id),
                    scope=owner.CUSTOMER_MFA_RESET_SCOPE,
                    reason=owner.CUSTOMER_MFA_RESET_REASON,
                ),
                account.subscriber_id,
                owner.CustomerKind.person,
                owner.AdminCustomerRecovery(account.actor_id),
            ),
        )


def request():
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/portal/auth/login",
            "headers": [],
            "client": ("127.0.0.1", 10000),
        }
    )


def login(factory, account):
    with factory() as db:
        return pa.complete_password_step(
            db,
            pa.VerifiedCredential(
                src=pa.SRC_USER_CREDENTIAL,
                principal_type="subscriber",
                principal_id=str(account.subscriber_id),
                audience="general",
                staff_binding=None,
                mfa_required=False,
                enrollment_required=False,
                credential_id=account.credential_id,
                credential_version=1,
                provider=AuthProvider.local,
                verified_hash="synthetic-phase-a-preverified",
            ),
            request=request(),
        )


def redeem(factory, account):
    with factory() as db:
        return pa.complete_mfa(
            db,
            pa.Challenge(
                typ="mfa",
                principal_id=str(account.subscriber_id),
                principal_type="subscriber",
                party_id=None,
                audience="general",
                src=pa.SRC_USER_CREDENTIAL,
                credential_id=account.credential_id,
                credential_version=1,
                access_credential_id=None,
                access_credential_updated_us=None,
            ),
            request=request(),
            method_id=account.method_id,
            totp_ok=False,
            recovery_code_hash=account.code_hash,
            failure_policy=pa.FailurePolicy(5, 15),
        )


def enroll(factory, account, operation):
    context = CommandContext.system(
        actor=f"subscriber:{account.subscriber_id}",
        scope=pa.SUBSCRIBER_MFA_SCOPE,
        reason="Subscriber self-service MFA settings",
    )
    proof = pa.SubscriberMfaSessionProof(
        account.session_id, account.subscriber_id, False
    )
    with factory() as db:
        if operation == "setup":
            return pa.setup_subscriber_mfa(
                db,
                command=pa.SetupSubscriberMfaCommand(
                    context,
                    account.subscriber_id,
                    "Synthetic enrollment",
                    pa.SubscriberMfaProvenance.CUSTOMER_WEB,
                    proof,
                ),
            )
        return pa.confirm_subscriber_mfa(
            db,
            command=pa.ConfirmSubscriberMfaCommand(
                context,
                account.subscriber_id,
                account.method_id,
                pyotp.TOTP(SECRET).now(),
                pa.SubscriberMfaProvenance.CUSTOMER_WEB,
                proof,
                pa.RecoveryCodeDelivery.RETURN_ONCE,
            ),
        )


def interleave(
    monkeypatch,
    engine,
    winner_module,
    winner_name,
    loser_module,
    loser_name,
    winner_operation,
    loser_operation,
):
    """Pause the winning owner after its real principal lock; observe exact wait."""
    reached, resume = Event(), Event()
    winner_pid, loser_pid = Queue(), Queue()
    real_winner, real_loser = (
        getattr(winner_module, winner_name),
        getattr(loser_module, loser_name),
    )

    def winning(db, *args, **kwargs):
        pid = db.scalar(text("SELECT pg_backend_pid()"))
        result = real_winner(db, *args, **kwargs)
        winner_pid.put(pid)
        reached.set()
        assert resume.wait(WAIT), "winner release timed out"
        return result

    def losing(db, *args, **kwargs):
        loser_pid.put(db.scalar(text("SELECT pg_backend_pid()")))
        return real_loser(db, *args, **kwargs)

    monkeypatch.setattr(winner_module, winner_name, winning)
    monkeypatch.setattr(loser_module, loser_name, losing)
    with ThreadPoolExecutor(max_workers=2) as pool:
        winner = pool.submit(winner_operation)
        try:
            assert reached.wait(WAIT), "winning principal lock not reached"
            loser = pool.submit(loser_operation)
            wait_for_exact_lock(
                engine,
                waiter_pid=loser_pid.get(timeout=WAIT),
                blocker_pid=winner_pid.get(timeout=WAIT),
            )
        finally:
            resume.set()
        winner_result = winner.result(timeout=WAIT)
        return winner_result, loser


def assert_removed(factory, account):
    with factory() as db:
        methods = list(
            db.scalars(
                select(MFAMethod).where(
                    MFAMethod.subscriber_id == account.subscriber_id
                )
            )
        )
        assert methods and all(
            not m.is_active and not m.enabled and not m.is_primary and m.secret is None
            for m in methods
        )
        assert not list(
            db.scalars(
                select(MFARecoveryCode)
                .join(MFAMethod)
                .where(
                    MFAMethod.subscriber_id == account.subscriber_id,
                    MFARecoveryCode.is_active.is_(True),
                )
            )
        )
        assert db.get(UserCredential, account.credential_id).credential_version == 1


def test_simultaneous_resets_have_one_transition_two_audits(
    monkeypatch, engine, factory, account
):
    reached, resume = Event(), Event()
    first_pid, second_pid = Queue(), Queue()
    real = owner._lock_principal

    def lock(db, *args, **kwargs):
        pid = db.scalar(text("SELECT pg_backend_pid()"))
        if current_thread().name.startswith("reset-first"):
            result = real(db, *args, **kwargs)
            first_pid.put(pid)
            reached.set()
            assert resume.wait(WAIT)
            return result
        second_pid.put(pid)
        return real(db, *args, **kwargs)

    monkeypatch.setattr(owner, "_lock_principal", lock)
    with (
        ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="reset-first"
        ) as first_pool,
        ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="reset-second"
        ) as second_pool,
    ):
        first = first_pool.submit(reset, factory, account)
        try:
            assert reached.wait(WAIT)
            second = second_pool.submit(reset, factory, account)
            wait_for_exact_lock(
                engine,
                waiter_pid=second_pid.get(timeout=WAIT),
                blocker_pid=first_pid.get(timeout=WAIT),
            )
        finally:
            resume.set()
        results = [first.result(timeout=WAIT), second.result(timeout=WAIT)]
    assert sorted(result.changed for result in results) == [False, True]
    assert sum(result.sessions_revoked for result in results) == 1
    with factory() as db:
        assert (
            len(
                list(
                    db.scalars(
                        select(AuditEvent).where(
                            AuditEvent.entity_id == str(account.subscriber_id),
                            AuditEvent.action == "auth.mfa_disabled",
                        )
                    )
                )
            )
            == 2
        )
        assert (
            len(
                list(
                    db.scalars(
                        select(EventStore).where(
                            EventStore.subscriber_id == account.subscriber_id
                        )
                    )
                )
            )
            == 1
        )
    assert_removed(factory, account)


def test_reset_lock_timeout_rolls_back(factory, account):
    with factory() as blocker:
        blocker.scalar(
            select(Subscriber)
            .where(Subscriber.id == account.subscriber_id)
            .with_for_update()
        )
        with pytest.raises(owner.CredentialAdministrationError) as refusal:
            reset(factory, account)
        assert (
            refusal.value.code == f"{owner.OWNER}.lock_timeout"
            and refusal.value.retryable
        )
        with factory() as check:
            assert check.get(MFAMethod, account.method_id).enabled
            assert check.get(MFARecoveryCode, account.code_id).is_active
            assert (
                check.get(AuthSession, account.session_id).status
                is SessionStatus.active
            )
        blocker.rollback()


@pytest.mark.parametrize("operation", ["setup", "confirm"])
@pytest.mark.parametrize("reset_first", [True, False])
def test_reset_enrollment_both_lock_orders(
    monkeypatch, engine, factory, account, operation, reset_first
):
    if operation == "confirm":
        with factory() as db:
            db.get(MFAMethod, account.method_id).enabled = False
            db.commit()
    if reset_first:
        result, loser = interleave(
            monkeypatch,
            engine,
            owner,
            "_lock_principal",
            pa,
            "_lock_subscriber_enrollment",
            lambda: reset(factory, account),
            lambda: enroll(factory, account, operation),
        )
        assert result.changed
        with pytest.raises(pa.PasswordAuthenticationError) as refusal:
            loser.result(timeout=WAIT)
        assert refusal.value.kind == "invalid_mfa_session"
    else:
        _, loser = interleave(
            monkeypatch,
            engine,
            pa,
            "_lock_subscriber_enrollment",
            owner,
            "_lock_principal",
            lambda: enroll(factory, account, operation),
            lambda: reset(factory, account),
        )
        assert loser.result(timeout=WAIT).changed
    assert_removed(factory, account)


@pytest.mark.parametrize("operation", ["login", "recovery"])
@pytest.mark.parametrize("reset_first", [True, False])
def test_reset_authentication_both_lock_orders(
    monkeypatch, engine, factory, account, operation, reset_first
):
    if operation == "login":
        # A preverified password-only source is consistent with disabled MFA.
        # Legacy secret/recovery residue still makes reset a security transition.
        with factory() as db:
            method = db.get(MFAMethod, account.method_id)
            method.enabled = method.is_active = method.is_primary = False
            db.commit()
    authenticate = lambda: (
        login(factory, account) if operation == "login" else redeem(factory, account)
    )
    if reset_first:
        result, loser = interleave(
            monkeypatch,
            engine,
            owner,
            "_lock_principal",
            pa,
            "_lock_principal_key_share",
            lambda: reset(factory, account),
            authenticate,
        )
        assert result.changed
        if operation == "recovery":
            with pytest.raises(pa.PasswordAuthenticationError) as refusal:
                loser.result(timeout=WAIT)
            assert refusal.value.kind == "invalid_mfa_code"
        else:
            # Reset changes MFA standing, not a universal password challenge epoch.
            loser.result(timeout=WAIT)
    else:
        _, loser = interleave(
            monkeypatch,
            engine,
            pa,
            "_lock_principal_key_share",
            owner,
            "_lock_principal",
            authenticate,
            lambda: reset(factory, account),
        )
        assert loser.result(timeout=WAIT).changed
    with factory() as db:
        sessions = list(
            db.scalars(
                select(AuthSession).where(
                    AuthSession.subscriber_id == account.subscriber_id
                )
            )
        )
        assert db.get(AuthSession, account.session_id).status is SessionStatus.revoked
        assert sum(s.status is SessionStatus.active for s in sessions) == (
            1 if operation == "login" and reset_first else 0
        )
    assert_removed(factory, account)
