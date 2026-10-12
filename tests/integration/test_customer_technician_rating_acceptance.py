"""First-feedback serialization on the Alembic-migrated PostgreSQL schema.

Run only through the dedicated-server integration runner. Separate connections
prove row-lock serialization; no SQLite or metadata schema synthesis is used.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier, Event
from time import monotonic, sleep
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.models.audit import AuditEvent
from app.models.dispatch import TechnicianProfile, WorkOrderAssignmentQueue
from app.models.event_store import EventStore
from app.models.subscriber import Reseller, ResellerUser, Subscriber
from app.models.work_order import WorkOrder
from app.services.customer_work_order_selfcare import (
    RateTechnicianCommand,
    RatingRepresentativeKind,
    ResellerRatingScope,
    TechnicianRatingError,
    rate_technician,
    recorded_rating_attribution,
)
from app.services.owner_commands import CommandContext
from tests.integration.test_field_lifecycle_acceptance import _customer, _user


def test_concurrent_customer_feedback_is_one_atomic_native_rating(cloned_database):
    engine = create_engine(cloned_database("heads"))
    barrier = Barrier(2)
    try:
        with Session(engine, expire_on_commit=False) as setup:
            user, customer = _user(setup), _customer(setup)
            technician = TechnicianProfile(
                system_user_id=user.id, person_id=user.id, is_active=True
            )
            job = WorkOrder(
                public_id=f"rating-acceptance-{uuid4()}",
                title="Synthetic native feedback visit",
                subscriber_id=customer.id,
                status="completed",
                work_order_kind="customer",
                is_active=True,
            )
            setup.add_all([technician, job])
            setup.flush()
            generation = uuid4()
            queue = WorkOrderAssignmentQueue(
                work_order_mirror_id=job.id,
                status="assigned",
                assigned_technician_id=technician.id,
                assignment_generation=generation,
                assignment_effective_at=datetime.now(UTC),
            )
            setup.add(queue)
            setup.flush()
            customer_id, public_id, job_id = customer.id, job.public_id, job.id
            technician_id, queue_id = technician.id, queue.id
            setup.commit()

        def submit(score):
            with Session(engine) as db:
                db.execute(text("SET statement_timeout = '15s'"))
                db.execute(text("SET lock_timeout = '10s'"))
                db.commit()
                command = RateTechnicianCommand(
                    context=CommandContext.system(
                        actor=f"subscriber:{customer_id}",
                        scope="customer:work_order:rating",
                        reason="Concurrent synthetic customer feedback",
                    ),
                    subscriber_id=customer_id,
                    work_order_public_id=public_id,
                    rating=score,
                    comment="Synthetic acceptance feedback",
                )
                barrier.wait(timeout=10)
                return rate_technician(db, command=command)

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(submit, (5, 1)))
        assert sorted(result.already_rated for result in outcomes) == [False, True]
        assert outcomes[0].rating == outcomes[1].rating
        with Session(engine) as verify:
            row = verify.get(WorkOrder, job_id)
            assert row is not None
            assert row.technician_rating == outcomes[0].rating
            attribution = recorded_rating_attribution(row)
            assert attribution is not None
            assert attribution.technician_id == technician_id
            assert attribution.queue_id == queue_id
            assert attribution.assignment_generation == generation
            audits = verify.scalars(
                select(AuditEvent).where(
                    AuditEvent.action == "work_order_technician_rated",
                    AuditEvent.entity_id == str(job_id),
                )
            ).all()
            assert len(audits) == 1
            events = verify.scalars(
                select(EventStore).where(EventStore.subscriber_id == customer_id)
            ).all()
            matching = [
                event
                for event in events
                if event.payload.get("name") == "work_order.technician_rated"
                and event.payload.get("work_order_id") == str(job_id)
            ]
            assert len(matching) == 1
            assert matching[0].payload["rating"] == row.technician_rating
    finally:
        engine.dispose()


@pytest.mark.parametrize("historical", [False, True])
def test_reseller_rating_waits_for_account_transfer_and_denies_stale_scope(
    cloned_database, historical
):
    """A pre-authorized representative cannot race a committed account transfer."""
    engine = create_engine(cloned_database("heads"))
    ready = Event()
    reader_pid = []
    try:
        with Session(engine, expire_on_commit=False) as setup:
            user, customer = _user(setup), _customer(setup)
            old_reseller = Reseller(name=f"Synthetic old rating reseller {uuid4()}")
            new_reseller = Reseller(name=f"Synthetic new rating reseller {uuid4()}")
            setup.add_all([old_reseller, new_reseller])
            setup.flush()
            representative = ResellerUser(reseller_id=old_reseller.id, is_active=True)
            technician = TechnicianProfile(
                system_user_id=user.id, person_id=user.id, is_active=True
            )
            customer.reseller_id = old_reseller.id
            job = WorkOrder(
                public_id=f"rating-transfer-{uuid4()}",
                title="Synthetic transfer rating",
                subscriber_id=customer.id,
                status="completed",
                work_order_kind="customer",
                is_active=True,
            )
            if historical:
                job.metadata_ = {"technician_rating": {"rating": 4}}
            setup.add_all([representative, technician, job])
            setup.flush()
            setup.add(
                WorkOrderAssignmentQueue(
                    work_order_mirror_id=job.id,
                    status="assigned",
                    assigned_technician_id=technician.id,
                    assignment_generation=uuid4(),
                    assignment_effective_at=datetime.now(UTC),
                )
            )
            setup.flush()
            customer_id, job_id, new_reseller_id = customer.id, job.id, new_reseller.id
            command = RateTechnicianCommand(
                context=CommandContext.system(
                    actor=f"reseller:{old_reseller.id}:principal:{representative.id}",
                    scope="customer:work_order:rating",
                    reason="Synthetic stale representative acceptance",
                ),
                subscriber_id=customer_id,
                work_order_public_id=job.public_id,
                rating=5,
                representative=ResellerRatingScope(
                    old_reseller.id,
                    representative.id,
                    RatingRepresentativeKind.reseller_user,
                ),
            )
            setup.commit()

        def submit_stale_scope():
            # Keep the physical connection checked out across read release so
            # pg_blocking_pids observes precisely this owner command.
            with (
                engine.connect() as connection,
                Session(connection, expire_on_commit=False) as db,
            ):
                db.execute(text("SET statement_timeout = '15s'"))
                db.execute(text("SET lock_timeout = '12s'"))
                cached = db.get(Subscriber, customer_id)
                assert cached is not None
                reader_pid.append(db.scalar(text("SELECT pg_backend_pid()")))
                db.commit()
                ready.set()
                try:
                    rate_technician(db, command=command)
                except TechnicianRatingError as exc:
                    return exc.code
                return "unexpected_success"

        with Session(engine) as transfer:
            account = transfer.scalar(
                select(Subscriber).where(Subscriber.id == customer_id).with_for_update()
            )
            assert account is not None
            account.reseller_id = new_reseller_id
            transfer.flush()
            transfer_pid = transfer.scalar(text("SELECT pg_backend_pid()"))
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(submit_stale_scope)
                try:
                    assert ready.wait(10)
                    deadline = monotonic() + 10
                    blocked = False
                    with engine.connect().execution_options(
                        isolation_level="AUTOCOMMIT"
                    ) as observer:
                        while monotonic() < deadline:
                            blockers = observer.scalar(
                                text("SELECT pg_blocking_pids(:pid)"),
                                {"pid": reader_pid[0]},
                            )
                            if transfer_pid in (blockers or []):
                                blocked = True
                                break
                            sleep(0.02)
                    assert blocked, (
                        "rating owner did not wait on subscriber transfer lock"
                    )
                    transfer.commit()
                    assert (
                        future.result(timeout=15)
                        == "customer.work_order_selfcare.work_order_not_found"
                    )
                finally:
                    transfer.rollback()
        with Session(engine) as verify:
            row = verify.get(WorkOrder, job_id)
            assert row is not None
            assert row.technician_rating == (4 if historical else None)
            assert (
                verify.scalars(
                    select(AuditEvent).where(
                        AuditEvent.action == "work_order_technician_rated",
                        AuditEvent.entity_id == str(job_id),
                    )
                ).all()
                == []
            )
            assert (
                verify.scalars(
                    select(EventStore).where(EventStore.subscriber_id == customer_id)
                ).all()
                == []
            )
    finally:
        engine.dispose()
