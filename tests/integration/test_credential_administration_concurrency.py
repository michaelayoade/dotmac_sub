"""Hosted PostgreSQL credential-admin lock/version proof; collect-only locally."""

import time
from concurrent.futures import ThreadPoolExecutor
from queue import Queue

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from app.models.auth import UserCredential
from app.models.subscriber import Subscriber
from app.services import credential_administration as owner
from app.services.owner_commands import CommandContext
from tests.integration import test_password_auth_credential_race as race
from tests.integration.test_password_auth_credential_race import (
    _login,
    _Pause,
    _pause_after_verify,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def factory(engine):
    return race.factory.__wrapped__(engine)


@pytest.fixture
def account(factory):
    yield from race.account.__wrapped__(factory)


@pytest.fixture(autouse=True)
def signing(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "credential-admin-concurrency-test")


def reset(factory, account):
    with factory() as db:
        return owner.update_credential(
            db,
            command=owner.UpdateCredential(
                CommandContext.system(
                    actor="service:credential-admin-concurrency",
                    scope=owner.SCOPE,
                    reason="verify principal credential lock order",
                ),
                account.credential_id,
                frozenset({owner.CredentialField.password}),
                password="Admin-Reset-Password-42!",
            ),
        )


def capture_owner_pid(monkeypatch) -> Queue[int]:
    pids: Queue[int] = Queue()
    real = owner._lock_principal

    def lock(db, kind, principal_id):
        pids.put(db.scalar(text("SELECT pg_backend_pid()")))
        return real(db, kind, principal_id)

    monkeypatch.setattr(owner, "_lock_principal", lock)
    return pids


def wait_for_exact_lock(engine, *, waiter_pid: int, blocker_pid: int) -> None:
    deadline = time.monotonic() + 3
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as observer:
        while time.monotonic() < deadline:
            blocked = observer.scalar(
                text(
                    "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE pid=:waiter AND wait_event_type='Lock' AND :blocker=ANY(pg_blocking_pids(pid)))"
                ),
                {"waiter": waiter_pid, "blocker": blocker_pid},
            )
            if blocked:
                return
            time.sleep(0.02)
    pytest.fail("the exact administrative backend did not wait on the named blocker")


def test_admin_waits_for_principal_and_trigger_increments_only_once(
    monkeypatch, engine, factory, account
):
    pids = capture_owner_pid(monkeypatch)
    with factory() as blocker:
        blocker_pid = blocker.scalar(text("SELECT pg_backend_pid()"))
        blocker.scalar(
            select(Subscriber)
            .where(Subscriber.id == account.subscriber_id)
            .with_for_update()
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(reset, factory, account)
            try:
                wait_for_exact_lock(
                    engine, waiter_pid=pids.get(timeout=3), blocker_pid=blocker_pid
                )
            finally:
                blocker.commit()
            out = result.result(timeout=15)
    assert out.credential_version == 2
    with factory() as check:
        assert check.get(UserCredential, account.credential_id).credential_version == 2


def test_admin_reset_between_password_verification_and_gate_denies_old_proof(
    monkeypatch, engine, factory, account
):
    pause = _Pause()
    _pause_after_verify(monkeypatch, pause, "admin-stale-login")
    with ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="admin-stale-login"
    ) as pool:
        result = pool.submit(_login, factory, account)
        try:
            assert pause.reached.wait(15)
            assert reset(factory, account).credential_version == 2
        finally:
            pause.resume.set()
        login = result.result(timeout=15)
    assert login[0] == "http" and login[1] == 401


def test_admin_principal_lock_timeout_rolls_back_without_version_change(
    factory, account
):
    with factory() as blocker:
        blocker.scalar(
            select(Subscriber)
            .where(Subscriber.id == account.subscriber_id)
            .with_for_update()
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(reset, factory, account)
            try:
                with pytest.raises(owner.CredentialAdministrationError) as exc:
                    result.result(timeout=10)
                assert exc.value.code.endswith("lock_timeout")
                assert exc.value.retryable
            finally:
                blocker.commit()
    with factory() as check:
        assert check.get(UserCredential, account.credential_id).credential_version == 1


def test_admin_holds_principal_before_waiting_for_credential(
    monkeypatch, engine, factory, account
):
    pids = capture_owner_pid(monkeypatch)
    with factory() as blocker:
        blocker_pid = blocker.scalar(text("SELECT pg_backend_pid()"))
        blocker.scalar(
            select(UserCredential)
            .where(UserCredential.id == account.credential_id)
            .with_for_update()
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(reset, factory, account)
            try:
                wait_for_exact_lock(
                    engine, waiter_pid=pids.get(timeout=3), blocker_pid=blocker_pid
                )
                with factory() as probe:
                    with pytest.raises(OperationalError) as exc:
                        probe.scalar(
                            select(Subscriber)
                            .where(Subscriber.id == account.subscriber_id)
                            .with_for_update(nowait=True)
                        )
                    assert (
                        getattr(exc.value.orig, "sqlstate", None)
                        or getattr(exc.value.orig, "pgcode", None)
                    ) == "55P03"
            finally:
                blocker.commit()
            assert result.result(timeout=15).credential_version == 2
