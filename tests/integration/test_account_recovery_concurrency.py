"""PostgreSQL proof that a first-use recovery key serializes before insert."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.orm import sessionmaker

from app.models.account_recovery import (
    AccountRecoveryCommandOutcome,
    AccountRecoveryRecord,
)
from app.models.audit import AuditEvent
from app.models.event_store import EventStore
from app.models.idempotency import IdempotencyKey
from app.models.subscriber import Reseller, Subscriber
from app.services import account_recovery as recovery_service
from app.services.account_recovery import (
    ACCOUNT_RECOVERY_WRITE_SCOPE,
    DeletionTombstone,
    RequestRecoverableDeletionCommand,
    request_recoverable_deletion,
)
from app.services.audit_adapter import AuditActor
from app.services.owner_commands import CommandContext


def test_same_key_concurrent_deletion_replays_one_tombstone(
    engine, monkeypatch
) -> None:
    """The account row lock covers the absent-key race on two real sessions."""
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    suffix = uuid4().hex[:12]
    with factory() as setup:
        reseller = Reseller(
            name=f"Recovery concurrency {suffix}",
            code=f"recovery-concurrency-{suffix}",
            is_active=True,
        )
        account = Subscriber(
            first_name="Recovery",
            last_name="Concurrency",
            email=f"recovery-concurrency-{suffix}@example.com",
            reseller=reseller,
        )
        setup.add(account)
        setup.commit()
        account_id = account.id
        reseller_id = reseller.id

    key = f"recovery-concurrency-{suffix}"
    first_locked = Event()
    release_first = Event()
    second_attempted = Event()
    second_acquired = Event()
    original_lock = recovery_service._lock_subscriber

    def observed_lock(db, locked_account_id):
        if not first_locked.is_set():
            subscriber = original_lock(db, locked_account_id)
            first_locked.set()
            assert release_first.wait(timeout=10)
            return subscriber
        second_attempted.set()
        subscriber = original_lock(db, locked_account_id)
        second_acquired.set()
        return subscriber

    def request() -> DeletionTombstone:
        command_id = uuid4()
        with factory() as db:
            outcome = request_recoverable_deletion(
                db,
                RequestRecoverableDeletionCommand(
                    account_id=account_id,
                    context=CommandContext(
                        command_id=command_id,
                        correlation_id=command_id,
                        actor="pytest:account-recovery",
                        scope=ACCOUNT_RECOVERY_WRITE_SCOPE,
                        reason="concurrent deletion replay proof",
                        idempotency_key=key,
                    ),
                    requested_by="pytest:account-recovery",
                    deleted_by="pytest:account-recovery",
                    audit_actor=AuditActor.user("pytest:account-recovery"),
                ),
            )
            assert isinstance(outcome, DeletionTombstone)
            return outcome

    try:
        with monkeypatch.context() as patch:
            patch.setattr(recovery_service, "_lock_subscriber", observed_lock)
            with ThreadPoolExecutor(max_workers=2) as pool:
                first_future = pool.submit(request)
                try:
                    assert first_locked.wait(timeout=10)
                    second_future = pool.submit(request)
                    assert second_attempted.wait(timeout=10)
                    assert not second_acquired.wait(timeout=0.25)
                finally:
                    release_first.set()
                first = first_future.result(timeout=20)
                second = second_future.result(timeout=20)

        assert second_acquired.is_set()
        assert first == second
        with factory() as check:
            records = check.scalars(
                select(AccountRecoveryRecord).where(
                    AccountRecoveryRecord.account_id == account_id
                )
            ).all()
            keys = check.scalars(
                select(IdempotencyKey).where(
                    IdempotencyKey.scope == "account_recovery:request_deletion",
                    IdempotencyKey.key == key,
                )
            ).all()
            assert len(records) == 1
            assert len(keys) == 1
    finally:
        release_first.set()
        with factory() as cleanup:
            cleanup.execute(
                delete(AuditEvent).where(
                    AuditEvent.action
                    == "customer.account_recovery.deletion_tombstoned",
                    AuditEvent.entity_id == str(account_id),
                )
            )
            cleanup.execute(
                delete(EventStore).where(EventStore.account_id == account_id)
            )
            cleanup.execute(
                delete(AccountRecoveryCommandOutcome).where(
                    AccountRecoveryCommandOutcome.account_id == account_id
                )
            )
            cleanup.execute(
                delete(IdempotencyKey).where(
                    IdempotencyKey.scope == "account_recovery:request_deletion",
                    IdempotencyKey.key == key,
                )
            )
            cleanup.execute(
                delete(AccountRecoveryRecord).where(
                    AccountRecoveryRecord.account_id == account_id
                )
            )
            subscriber = cleanup.get(Subscriber, account_id)
            if subscriber is not None:
                cleanup.delete(subscriber)
            reseller = cleanup.get(Reseller, reseller_id)
            if reseller is not None:
                cleanup.delete(reseller)
            cleanup.commit()
