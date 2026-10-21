"""Real migrated PostgreSQL selected-person races; dedicated test runner only."""

from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine
from sqlalchemy.orm import Session

from app.models.dispatch import WorkOrderAssignmentQueue
from app.models.field_vendor import FieldVendorUser
from app.models.system_user import SystemUser
from app.models.work_order import WorkOrder
from app.schemas.field import FieldTransitionResponse
from app.services.field.execution_contracts import ApplyFieldTransition, FieldEvent
from app.services.field.transitions import field_transitions
from app.services.field.work_order_access import FieldAccessError
from app.services.work_order_assignment_contracts import (
    VendorAssignmentTarget,
    WorkOrderAssignmentOutcome,
)
from app.services.work_order_commands import work_order_commands
from app.services.work_order_errors import WorkOrderCommandError
from tests.test_vendor_attending_person import (  # noqa: F401
    Fixture,
    command,
    context,
)
from tests.test_vendor_attending_person import (
    visit as selected_visit_fixture,
)

visit = selected_visit_fixture


@pytest.fixture
def engine(cloned_database: Callable[[str], URL]) -> Iterator[Engine]:
    """Private real-migrated database; all sessions share physical commits."""
    standalone_engine = create_engine(cloned_database("heads"))
    try:
        yield standalone_engine
    finally:
        standalone_engine.dispose()


@pytest.fixture
def db_session(engine: Engine) -> Iterator[Session]:
    # Deliberately avoid the root fixture's outer transaction/savepoint: race
    # workers must observe committed fixture rows on independent connections.
    with Session(engine, expire_on_commit=False) as session:
        yield session


RaceResult = (
    WorkOrderAssignmentOutcome
    | FieldTransitionResponse
    | WorkOrderCommandError
    | FieldAccessError
)


def _race(
    engine: Engine,
    operations: Sequence[
        Callable[[Session], WorkOrderAssignmentOutcome | FieldTransitionResponse]
    ],
) -> list[RaceResult]:
    barrier = Barrier(len(operations))

    def run(
        operation: Callable[
            [Session], WorkOrderAssignmentOutcome | FieldTransitionResponse
        ],
    ) -> RaceResult:
        with Session(engine) as db:
            # Configure bounded waits without leaving an active caller transaction.
            db.execute(text("SET lock_timeout = '5s'"))
            db.execute(text("SET statement_timeout = '15s'"))
            db.commit()
            barrier.wait(timeout=10)
            try:
                return operation(db)
            except (WorkOrderCommandError, FieldAccessError) as exc:
                return exc

    with ThreadPoolExecutor(max_workers=len(operations)) as pool:
        futures = [pool.submit(run, operation) for operation in operations]
        return [future.result(timeout=25) for future in futures]


def test_competing_selection_has_one_winner_and_one_stale_command(
    engine, db_session, visit: Fixture
):
    first = work_order_commands.assign(
        db_session, command=command(visit), context=context(visit)
    )
    base = replace(
        command(visit),
        expected_revision=first.revision,
        expected_assignment_generation=first.assignment_generation,
    )
    alternative = replace(
        base, target=VendorAssignmentTarget(visit.vendor_id, visit.second_member)
    )
    # Release any main-session observation before independent write sessions.
    db_session.commit()
    assert not db_session.in_transaction()
    results = _race(
        engine,
        [
            lambda db: work_order_commands.assign(
                db, command=base, context=context(visit)
            ),
            lambda db: work_order_commands.assign(
                db, command=alternative, context=context(visit)
            ),
        ],
    )
    assert sum(isinstance(result, WorkOrderCommandError) for result in results) == 1
    assert (
        next(
            result for result in results if isinstance(result, WorkOrderCommandError)
        ).code
        == "stale_assignment"
    )


def test_old_member_transition_races_reselection_then_revokes_replay(
    engine, db_session, visit: Fixture
):
    first = work_order_commands.assign(
        db_session, command=command(visit), context=context(visit)
    )
    selection = replace(
        command(visit),
        target=VendorAssignmentTarget(visit.vendor_id, visit.second_member),
        expected_revision=first.revision,
        expected_assignment_generation=first.assignment_generation,
    )
    transition = ApplyFieldTransition(
        context=context(visit, visit.first_user, scope="field"),
        requester_system_user_id=visit.first_user,
        public_id=visit.public_id,
        event=FieldEvent.start,
        client_event_id=uuid4(),
        assignment_generation=first.assignment_generation,
    )
    # Release any main-session observation before independent write sessions.
    db_session.commit()
    assert not db_session.in_transaction()
    results = _race(
        engine,
        [
            lambda db: work_order_commands.assign(
                db, command=selection, context=context(visit)
            ),
            lambda db: field_transitions.apply(db, transition),
        ],
    )
    assert not all(isinstance(result, Exception) for result in results)
    # If execution won first, dispatch must refresh its expected revision.
    db_session.expire_all()
    row = db_session.query(WorkOrder).filter_by(public_id=visit.public_id).one()
    revision = row.updated_at
    queue = (
        db_session.query(WorkOrderAssignmentQueue)
        .filter_by(work_order_mirror_id=row.id, status="assigned")
        .one()
    )
    generation, selected = queue.assignment_generation, queue.assigned_vendor_user_id
    db_session.commit()
    if selected != visit.second_member:
        work_order_commands.assign(
            db_session,
            command=replace(
                selection,
                expected_revision=revision,
                expected_assignment_generation=generation,
            ),
            context=context(visit),
        )
    with pytest.raises(FieldAccessError):
        field_transitions.apply(db_session, transition)


def test_opposite_member_swaps_on_distinct_visits_have_no_deadlock(
    engine, db_session, visit: Fixture
):
    first = work_order_commands.assign(
        db_session, command=command(visit), context=context(visit)
    )
    other_staff = SystemUser(
        first_name="Synthetic",
        last_name="Other dispatcher",
        email=f"{uuid4()}@example.invalid",
        is_active=True,
    )
    second_job = WorkOrder(
        public_id=f"attending-race-{uuid4()}",
        title="Second synthetic infrastructure visit",
        work_order_kind="infrastructure",
        status="scheduled",
        is_active=True,
    )
    db_session.add_all([other_staff, second_job])
    db_session.flush()
    second_fixture = replace(
        visit,
        public_id=second_job.public_id,
        revision=second_job.updated_at,
        staff_user=other_staff.id,
    )
    db_session.commit()
    second_base = replace(
        command(second_fixture),
        target=VendorAssignmentTarget(visit.vendor_id, visit.second_member),
    )
    second = work_order_commands.assign(
        db_session, command=second_base, context=context(second_fixture)
    )
    swap_first = replace(
        command(visit),
        target=VendorAssignmentTarget(visit.vendor_id, visit.second_member),
        expected_revision=first.revision,
        expected_assignment_generation=first.assignment_generation,
    )
    swap_second = replace(
        second_base,
        target=VendorAssignmentTarget(visit.vendor_id, visit.first_member),
        expected_revision=second.revision,
        expected_assignment_generation=second.assignment_generation,
    )
    # Release any main-session observation before independent write sessions.
    db_session.commit()
    assert not db_session.in_transaction()
    results = _race(
        engine,
        [
            lambda db: work_order_commands.assign(
                db, command=swap_first, context=context(visit)
            ),
            lambda db: work_order_commands.assign(
                db, command=swap_second, context=context(second_fixture)
            ),
        ],
    )
    assert not any(isinstance(result, Exception) for result in results)


def test_inactive_selected_member_fails_replay_admission(db_session, visit: Fixture):
    assigned = work_order_commands.assign(
        db_session, command=command(visit), context=context(visit)
    )
    transition = ApplyFieldTransition(
        context=context(visit, visit.first_user, scope="field"),
        requester_system_user_id=visit.first_user,
        public_id=visit.public_id,
        event=FieldEvent.start,
        client_event_id=uuid4(),
        assignment_generation=assigned.assignment_generation,
    )
    field_transitions.apply(db_session, transition)
    db_session.get(FieldVendorUser, visit.first_member).is_active = False
    db_session.commit()
    with pytest.raises(FieldAccessError):
        field_transitions.apply(db_session, transition)
