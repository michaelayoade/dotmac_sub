"""Bounded PostgreSQL proof of transition/identity lock ordering and freshness.

The canonical assignment preparation holds the production principal->visit lock sequence. No
business transition is synthesized and no object-storage transport is called.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event
from time import monotonic
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.models.dispatch import TechnicianProfile, WorkOrderAssignmentQueue
from app.models.work_order import WorkOrder
from app.services import customer_field_identity as identity
from app.services.work_order_assignment_contracts import (
    TechnicianAssignmentTarget,
    WorkOrderAssignmentPreparationQuery,
)
from app.services.work_order_commands import work_order_commands
from tests.integration.test_field_lifecycle_acceptance import (
    _assignment_access,
    _customer,
    _user,
)


@pytest.mark.parametrize("rotate_generation", [False, True])
def test_identity_waits_on_subject_before_visit_and_rechecks_assignment(
    cloned_database, rotate_generation
):
    engine = create_engine(cloned_database("heads"))
    ready = Event()
    reader_pid = []
    try:
        with Session(engine, expire_on_commit=False) as setup:
            user, subscriber = _user(setup), _customer(setup)
            profile = TechnicianProfile(
                system_user_id=user.id, person_id=user.id, is_active=True
            )
            job = WorkOrder(
                public_id=f"identity-lock-test-{uuid4()}",
                title="Synthetic identity lock scheduling",
                subscriber_id=subscriber.id,
                work_order_kind="customer",
                status="scheduled",
                is_active=True,
            )
            setup.add_all([profile, job])
            setup.flush()
            queue = WorkOrderAssignmentQueue(
                work_order_mirror_id=job.id,
                status="assigned",
                assigned_technician_id=profile.id,
                assignment_generation=uuid4(),
                assignment_effective_at=datetime.now(UTC),
            )
            setup.add(queue)
            setup.flush()
            query = identity.CustomerFieldIdentityQuery(subscriber.id, job.id)
            user_id, queue_id, public_id = user.id, queue.id, job.public_id
            setup.commit()

        def read_identity():
            with Session(engine) as reader:
                reader.execute(text("SET LOCAL statement_timeout = '12s'"))
                # Deliberately retain a stale identity-map row across the wait.
                cached = reader.get(WorkOrderAssignmentQueue, queue_id)
                assert cached is not None
                reader_pid.append(reader.scalar(text("SELECT pg_backend_pid()")))
                ready.set()
                return identity.card(reader, query=query)

        with Session(engine) as transition:
            transition.execute(text("SET LOCAL statement_timeout = '12s'"))
            transition_pid = transition.scalar(text("SELECT pg_backend_pid()"))
            preparation = work_order_commands.prepare_assignment(
                transition,
                query=WorkOrderAssignmentPreparationQuery(
                    work_order_public_id=public_id,
                    target=TechnicianAssignmentTarget(profile.id),
                    access=_assignment_access(user_id),
                ),
            )
            assert user_id in preparation.principal_ids
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(read_identity)
                try:
                    assert ready.wait(10), "identity reader did not start"
                    deadline = monotonic() + 10
                    with engine.connect().execution_options(
                        isolation_level="AUTOCOMMIT"
                    ) as observer:
                        while monotonic() < deadline:
                            blocked = (
                                observer.execute(
                                    text(
                                        "SELECT query, pg_blocking_pids(pid) AS blockers "
                                        "FROM pg_stat_activity WHERE pid=:pid "
                                        "AND wait_event_type='Lock'"
                                    ),
                                    {"pid": reader_pid[0]},
                                )
                                .mappings()
                                .one_or_none()
                            )
                            if blocked is not None:
                                break
                            Event().wait(0.02)
                        else:
                            pytest.fail(
                                "identity reader never waited on transition user lock"
                            )
                    assert "system_users" in blocked["query"]
                    assert transition_pid in blocked["blockers"]
                    # This would time out/deadlock under the old visit->user read.
                    transition.execute(text("SET LOCAL statement_timeout = '2s'"))
                    transition.scalar(
                        select(WorkOrder)
                        .where(WorkOrder.id == query.work_order_id)
                        .with_for_update()
                    )
                    if rotate_generation:
                        current = transition.get(WorkOrderAssignmentQueue, queue_id)
                        current.assignment_generation = uuid4()
                    transition.commit()
                    result = future.result(timeout=15)
                    assert result.card is None
                    expected = (
                        identity.IdentityUnavailableReason.identity_unavailable
                        if rotate_generation
                        else identity.IdentityUnavailableReason.identity_not_approved
                    )
                    assert result.unavailable_reason == expected
                finally:
                    transition.rollback()
    finally:
        engine.dispose()
