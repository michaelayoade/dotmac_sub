"""Hosted migrated-PostgreSQL lock and unique arbitration proof; collect locally."""

from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.models.notification import (
    CommunicationIntentRecord,
    Notification,
    NotificationIntentCoverage,
)
from app.models.subscriber import Subscriber
from app.services import email_verification as owner
from app.services.audit_adapter import AuditActor
from app.services.owner_commands import CommandContext
from app.services.subscriber import _default_reseller_id
from tests.integration.test_credential_administration_concurrency import (
    wait_for_exact_lock,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def factory(engine):
    assert engine.dialect.name == "postgresql"
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def target(factory):
    with factory() as setup:
        row = Subscriber(
            first_name="Verification",
            last_name="Concurrency",
            email=f"verification-{uuid4().hex}@example.test",
            reseller_id=_default_reseller_id(setup),
        )
        setup.add(row)
        setup.commit()
        subscriber_id = row.id
    try:
        yield subscriber_id
    finally:
        with factory() as cleanup:
            # Coverage deliberately RESTRICTs notification deletion (migration637).
            # Remove only coverage for this test-created Subscriber's deliveries.
            cleanup.execute(
                delete(NotificationIntentCoverage).where(
                    NotificationIntentCoverage.notification_id.in_(
                        select(Notification.id).where(
                            Notification.subscriber_id == subscriber_id
                        )
                    )
                )
            )
            cleanup.execute(
                delete(Notification).where(Notification.subscriber_id == subscriber_id)
            )
            cleanup.execute(
                delete(CommunicationIntentRecord).where(
                    CommunicationIntentRecord.subscriber_id == subscriber_id
                )
            )
            cleanup.execute(delete(Subscriber).where(Subscriber.id == subscriber_id))
            cleanup.commit()


def _command(target):
    return owner.RequestEmailVerificationCommand(
        context=CommandContext.system(
            actor=f"subscriber:{target}",
            scope=owner.EMAIL_VERIFICATION_SCOPE,
            reason="Hosted verification request lock proof",
            idempotency_key=str(uuid4()),
        ),
        subscriber_id=target,
        actor=AuditActor.user(str(target)),
    )


def test_owner_waits_on_exact_subscriber_lock_and_reloads_changed_email(
    factory, target, engine, monkeypatch
):
    pids: Queue[int] = Queue()
    original = owner._subscriber

    def capture(db, subscriber_id, *, lock):
        if lock:
            pids.put(db.scalar(text("SELECT pg_backend_pid()")))
        return original(db, subscriber_id, lock=lock)

    monkeypatch.setattr(owner, "_subscriber", capture)
    command = _command(target)

    def request():
        with factory() as db:
            return owner.request_email_verification(db, command=command)

    with factory() as blocker:
        blocker_pid = blocker.scalar(text("SELECT pg_backend_pid()"))
        subscriber = blocker.scalar(
            select(Subscriber).where(Subscriber.id == target).with_for_update()
        )
        subscriber.email = "changed-under-lock@example.test"
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(request)
            try:
                wait_for_exact_lock(
                    engine, waiter_pid=pids.get(timeout=3), blocker_pid=blocker_pid
                )
            finally:
                blocker.commit()
            outcome = result.result(timeout=15)
    assert outcome.queued
    with factory() as check:
        notification = check.scalar(
            select(Notification).where(Notification.subscriber_id == target)
        )
        assert notification.recipient == "changed-under-lock@example.test"


def test_concurrent_same_request_converges_to_one_intent(factory, target):
    command = _command(target)
    barrier = Barrier(2)

    def request():
        with factory() as db:
            barrier.wait(timeout=5)
            return owner.request_email_verification(db, command=command)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(request) for _ in range(2)]
        results = [future.result(timeout=15) for future in futures]
    assert results[0].intent_id == results[1].intent_id
    assert sorted(result.replayed for result in results) == [False, True]
    with factory() as check:
        assert (
            len(
                check.scalars(
                    select(CommunicationIntentRecord).where(
                        CommunicationIntentRecord.subscriber_id == target
                    )
                ).all()
            )
            == 1
        )
        assert (
            len(
                check.scalars(
                    select(Notification).where(Notification.subscriber_id == target)
                ).all()
            )
            == 1
        )


def test_migrated_dedupe_unique_constraint_arbitrates_competing_inserts(factory):
    key = f"verification-unique-proof:{uuid4()}"
    barrier = Barrier(2)

    def insert():
        with factory() as db:
            db.add(
                CommunicationIntentRecord(
                    event_type="auth.email_verification",
                    category="account",
                    communication_class="transactional",
                    channels=["email"],
                    include_reseller=False,
                    status="pending",
                    suppression_reasons=[],
                    dedupe_key=key,
                    metadata_={},
                )
            )
            barrier.wait(timeout=5)
            try:
                db.commit()
                return "committed"
            except IntegrityError as exc:
                db.rollback()
                assert (
                    exc.orig.diag.constraint_name
                    == "uq_communication_intents_dedupe_key"
                )
                return "conflict"

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(insert) for _ in range(2)]
            assert sorted(future.result(timeout=15) for future in futures) == [
                "committed",
                "conflict",
            ]
    finally:
        with factory() as cleanup:
            cleanup.execute(
                delete(CommunicationIntentRecord).where(
                    CommunicationIntentRecord.dedupe_key == key
                )
            )
            cleanup.commit()


def test_resend_times_out_without_discarding_callers_flushed_target_write(
    factory, target, monkeypatch
):
    """Real independent sessions reproduce the caller/owner self-block safely."""
    from contextlib import contextmanager
    from types import SimpleNamespace

    from fastapi import HTTPException

    from app.api import auth_flow as api
    from app.models.audit import AuditEvent
    from app.services import rate_limiter_adapter
    from app.services.db_session_adapter import db_session_adapter

    @contextmanager
    def independent():
        with factory() as db:
            yield db

    monkeypatch.setattr(db_session_adapter, "owner_command_session", independent)
    monkeypatch.setattr(
        rate_limiter_adapter,
        "allow_operation",
        lambda *args, **kwargs: SimpleNamespace(allowed=True),
    )
    with factory() as caller:
        subscriber = caller.get(Subscriber, target)
        subscriber.first_name = "Keep this flushed caller write"
        caller.flush()
        transaction = caller.get_transaction()
        with pytest.raises(HTTPException) as captured:
            api.resend_verification_email_endpoint(
                auth={"principal_id": str(target), "principal_type": "subscriber"},
                db=caller,
            )
        assert captured.value.status_code == 503
        assert captured.value.headers == {"Retry-After": "5"}
        assert caller.get_transaction() is transaction
        assert subscriber.first_name == "Keep this flushed caller write"
        # Prove the caller can still complete its own transaction.
        caller.commit()
    with factory() as check:
        assert (
            check.get(Subscriber, target).first_name == "Keep this flushed caller write"
        )
        assert (
            check.scalars(
                select(CommunicationIntentRecord).where(
                    CommunicationIntentRecord.subscriber_id == target
                )
            ).all()
            == []
        )
        assert (
            check.scalars(
                select(AuditEvent).where(
                    AuditEvent.entity_id == str(target),
                    AuditEvent.action == "auth.email_verification_requested",
                )
            ).all()
            == []
        )


def test_cross_subscriber_key_race_has_one_owner_and_no_loser_audit(
    factory, target, monkeypatch
):
    from dataclasses import replace

    from app.models.audit import AuditEvent
    from app.services import communication_intents

    with factory() as setup:
        other = Subscriber(
            first_name="Other",
            last_name="Verification",
            email=f"other-verification-{uuid4().hex}@example.test",
            reseller_id=_default_reseller_id(setup),
        )
        setup.add(other)
        setup.commit()
        other_id = other.id
    first = _command(target)
    second = replace(
        first, subscriber_id=other_id, actor=AuditActor.user(str(other_id))
    )
    barrier = Barrier(2)
    original = communication_intents.submit

    def concurrent_submit(*args, **kwargs):
        barrier.wait(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr(communication_intents, "submit", concurrent_submit)

    def request(command):
        with factory() as db:
            try:
                outcome = owner.request_email_verification(db, command=command)
                return (command.subscriber_id, outcome)
            except owner.EmailVerificationError as exc:
                assert exc.code == "auth.email_verification.idempotency_conflict"
                assert not db.in_transaction()
                return (command.subscriber_id, None)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(request, command) for command in (first, second)]
            results = [future.result(timeout=15) for future in futures]
        winners = [
            subscriber_id for subscriber_id, outcome in results if outcome is not None
        ]
        losers = [
            subscriber_id for subscriber_id, outcome in results if outcome is None
        ]
        assert len(winners) == len(losers) == 1
        with factory() as check:
            records = check.scalars(
                select(CommunicationIntentRecord).where(
                    CommunicationIntentRecord.subscriber_id.in_([target, other_id])
                )
            ).all()
            assert len(records) == 1 and records[0].subscriber_id == winners[0]
            assert (
                check.scalars(
                    select(AuditEvent).where(
                        AuditEvent.entity_id == str(losers[0]),
                        AuditEvent.action == "auth.email_verification_requested",
                    )
                ).all()
                == []
            )
            assert (
                check.scalars(
                    select(Notification).where(Notification.subscriber_id == losers[0])
                ).all()
                == []
            )
    finally:
        with factory() as cleanup:
            # Coverage deliberately RESTRICTs notification deletion (migration637).
            # Remove only coverage for this test-created Subscriber's deliveries.
            cleanup.execute(
                delete(NotificationIntentCoverage).where(
                    NotificationIntentCoverage.notification_id.in_(
                        select(Notification.id).where(
                            Notification.subscriber_id == other_id
                        )
                    )
                )
            )
            cleanup.execute(
                delete(Notification).where(Notification.subscriber_id == other_id)
            )
            cleanup.execute(
                delete(CommunicationIntentRecord).where(
                    CommunicationIntentRecord.subscriber_id == other_id
                )
            )
            cleanup.execute(delete(Subscriber).where(Subscriber.id == other_id))
            cleanup.commit()
