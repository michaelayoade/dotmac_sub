"""Fast unit lane for explicit attending membership; no deployed-schema claims."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

from app.models.dispatch import WorkOrderAssignmentQueue
from app.models.field_vendor import FieldVendor, FieldVendorUser
from app.models.system_user import SystemUser
from app.models.vendor_routes import Vendor
from app.models.work_order import WorkOrder
from app.services.field.execution_contracts import (
    ApplyFieldTransition,
    FieldEvent,
    FieldJobsQuery,
)
from app.services.field.jobs import field_jobs
from app.services.field.transitions import field_transitions
from app.services.field.work_order_access import (
    FieldAccessError,
    FieldWorkOrderScope,
    ResolveFieldActor,
    require_work_order,
    resolve_field_actor,
)
from app.services.owner_commands import CommandContext
from app.services.permission_admission import PermissionAdmission
from app.services.work_order_assignment_contracts import (
    AssignmentOperation,
    VendorAssignmentCandidatesQuery,
    VendorAssignmentTarget,
    WorkOrderAssignmentCommand,
)
from app.services.work_order_commands import work_order_commands
from app.services.work_order_errors import WorkOrderCommandError


@dataclass(frozen=True)
class Fixture:
    public_id: str
    vendor_id: UUID
    bridge_id: UUID
    first_user: UUID
    first_member: UUID
    second_user: UUID
    second_member: UUID
    revision: datetime
    staff_user: UUID


@pytest.fixture
def visit(db_session: Session) -> Fixture:
    vendor = Vendor(name="Synthetic attending contractor", is_active=True)
    users = [
        SystemUser(
            first_name="Synthetic",
            last_name=f"Crew{i}",
            email=f"{uuid4()}@example.invalid",
            is_active=True,
        )
        for i in range(2)
    ]
    staff = SystemUser(
        first_name="Synthetic",
        last_name="Dispatcher",
        email=f"{uuid4()}@example.invalid",
        is_active=True,
    )
    db_session.add_all([vendor, *users, staff])
    db_session.flush()
    bridge = FieldVendor(name=vendor.name, native_vendor_id=vendor.id, is_active=True)
    job = WorkOrder(
        public_id=f"attending-{uuid4()}",
        title="Synthetic infrastructure visit",
        work_order_kind="infrastructure",
        status="scheduled",
        is_active=True,
    )
    db_session.add_all([bridge, job])
    db_session.flush()
    members = [
        FieldVendorUser(
            vendor_id=bridge.id,
            system_user_id=user.id,
            role="owner" if i else "field",
            is_active=True,
        )
        for i, user in enumerate(users)
    ]
    db_session.add_all(members)
    db_session.flush()
    result = Fixture(
        job.public_id,
        vendor.id,
        bridge.id,
        users[0].id,
        members[0].id,
        users[1].id,
        members[1].id,
        job.updated_at,
        staff.id,
    )
    db_session.commit()
    db_session.refresh(job)
    result = replace(result, revision=job.updated_at)
    db_session.commit()
    return result


def context(
    visit: Fixture,
    actor: UUID | None = None,
    *,
    scope: str = "operations:dispatch:assign",
) -> CommandContext:
    return CommandContext.system(
        actor=str(actor or visit.staff_user),
        scope=scope,
        reason="Synthetic staff dispatch",
        idempotency_key=str(uuid4()),
    )


def command(visit: Fixture) -> WorkOrderAssignmentCommand:
    return WorkOrderAssignmentCommand(
        work_order_public_id=visit.public_id,
        access=PermissionAdmission(
            visit.staff_user, "system_user", frozenset({"operations:dispatch:assign"})
        ),
        target=VendorAssignmentTarget(visit.vendor_id, visit.first_member),
        expected_revision=visit.revision,
    )


def test_exact_selected_person_and_owner_is_not_a_bypass(
    db_session: Session, visit: Fixture
) -> None:
    outcome = work_order_commands.assign(
        db_session, command=command(visit), context=context(visit)
    )
    assert outcome.target == VendorAssignmentTarget(visit.vendor_id, visit.first_member)
    assert [
        job.id for job in field_jobs.list(db_session, FieldJobsQuery(visit.first_user))
    ] == [visit.public_id]
    assert field_jobs.list(db_session, FieldJobsQuery(visit.second_user)) == []
    owner = resolve_field_actor(db_session, ResolveFieldActor(visit.second_user))
    with pytest.raises(FieldAccessError):
        require_work_order(db_session, FieldWorkOrderScope(owner, visit.public_id))


def test_reselection_rotates_generation_and_omitted_reschedule_preserves(
    db_session: Session, visit: Fixture
) -> None:
    first = work_order_commands.assign(
        db_session, command=command(visit), context=context(visit)
    )
    second = work_order_commands.assign(
        db_session,
        command=replace(
            command(visit),
            target=VendorAssignmentTarget(visit.vendor_id, visit.second_member),
            expected_revision=first.revision,
            expected_assignment_generation=first.assignment_generation,
        ),
        context=context(visit),
    )
    assert second.assignment_generation != first.assignment_generation
    scheduled = work_order_commands.assign(
        db_session,
        command=replace(
            command(visit),
            operation=AssignmentOperation.reschedule,
            target=VendorAssignmentTarget(visit.vendor_id),
            expected_revision=second.revision,
            expected_assignment_generation=second.assignment_generation,
            scheduled_start=second.revision + timedelta(days=1),
        ),
        context=context(visit),
    )
    assert scheduled.assignment_generation == second.assignment_generation
    assert scheduled.target == VendorAssignmentTarget(
        visit.vendor_id, visit.second_member
    )
    assert field_jobs.list(db_session, FieldJobsQuery(visit.first_user)) == []


def test_replay_is_historical_and_changed_member_conflicts(
    db_session: Session, visit: Fixture
) -> None:
    ctx = context(visit)
    cmd = command(visit)
    first = work_order_commands.assign(db_session, command=cmd, context=ctx)
    second = work_order_commands.assign(
        db_session,
        command=replace(
            cmd,
            target=VendorAssignmentTarget(visit.vendor_id, visit.second_member),
            expected_revision=first.revision,
            expected_assignment_generation=first.assignment_generation,
        ),
        context=context(visit),
    )
    replay = work_order_commands.assign(db_session, command=cmd, context=ctx)
    assert (
        replay.replayed and replay.assignment_generation == first.assignment_generation
    )
    assert replay.target != second.target
    with pytest.raises(WorkOrderCommandError, match="different content"):
        work_order_commands.assign(
            db_session,
            command=replace(
                cmd, target=VendorAssignmentTarget(visit.vendor_id, visit.second_member)
            ),
            context=ctx,
        )


@pytest.mark.parametrize(
    "fault",
    [
        "missing_selection",
        "inactive_user",
        "inactive_member",
        "inactive_vendor",
        "inactive_bridge",
        "cross_vendor",
        "ambiguous",
    ],
)
def test_invalid_selection_fails_closed(
    db_session: Session, visit: Fixture, fault: str
) -> None:
    cmd = command(visit)
    if fault == "missing_selection":
        cmd = replace(cmd, target=VendorAssignmentTarget(visit.vendor_id))
    elif fault == "inactive_user":
        db_session.get(SystemUser, visit.first_user).is_active = False
    elif fault == "inactive_member":
        db_session.get(FieldVendorUser, visit.first_member).is_active = False
    elif fault == "inactive_vendor":
        db_session.get(Vendor, visit.vendor_id).is_active = False
    elif fault == "inactive_bridge":
        db_session.get(FieldVendor, visit.bridge_id).is_active = False
    elif fault == "cross_vendor":
        other = Vendor(name="Unrelated vendor", is_active=True)
        db_session.add(other)
        db_session.flush()
        cmd = replace(cmd, target=VendorAssignmentTarget(other.id, visit.first_member))
    elif fault == "ambiguous":
        other_bridge = FieldVendor(name="Ambiguous bridge", is_active=True)
        db_session.add(other_bridge)
        db_session.flush()
        db_session.add(
            FieldVendorUser(
                vendor_id=other_bridge.id,
                system_user_id=visit.first_user,
                is_active=True,
            )
        )
    db_session.commit()
    with pytest.raises(WorkOrderCommandError):
        work_order_commands.assign(db_session, command=cmd, context=context(visit))
    assert db_session.query(WorkOrderAssignmentQueue).count() == 0


def test_staff_permission_and_vendor_principal_separation(
    db_session: Session, visit: Fixture
) -> None:
    with pytest.raises(WorkOrderCommandError, match="permission"):
        work_order_commands.assign(
            db_session,
            command=replace(
                command(visit),
                access=PermissionAdmission(
                    visit.staff_user, "system_user", frozenset()
                ),
            ),
            context=context(visit, scope="operations:dispatch:assign"),
        )
    with pytest.raises(WorkOrderCommandError, match="active staff"):
        work_order_commands.assign(
            db_session,
            command=replace(
                command(visit),
                access=PermissionAdmission(
                    visit.first_user,
                    "system_user",
                    frozenset({"operations:dispatch:assign"}),
                ),
            ),
            context=context(visit, visit.first_user),
        )


def test_revocation_blocks_cached_actor_and_execution_replay(
    db_session: Session, visit: Fixture
) -> None:
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
    actor = resolve_field_actor(db_session, ResolveFieldActor(visit.first_user))
    db_session.get(FieldVendorUser, visit.first_member).is_active = False
    db_session.commit()
    with pytest.raises(FieldAccessError):
        require_work_order(
            db_session, FieldWorkOrderScope(actor, visit.public_id, lock=True)
        )
    db_session.rollback()
    with pytest.raises(FieldAccessError):
        field_transitions.apply(db_session, transition)


def test_candidates_and_legacy_unselected_do_not_grant_execution(
    db_session: Session, visit: Fixture
) -> None:
    result = work_order_commands.vendor_assignment_candidates(
        db_session,
        query=VendorAssignmentCandidatesQuery(visit.public_id, visit.vendor_id),
    )
    assert {candidate.vendor_user_id for candidate in result.candidates} == {
        visit.first_member,
        visit.second_member,
    }
    job = db_session.query(WorkOrder).filter_by(public_id=visit.public_id).one()
    db_session.add(
        WorkOrderAssignmentQueue(
            work_order_mirror_id=job.id,
            assigned_vendor_id=visit.vendor_id,
            status="assigned",
        )
    )
    db_session.commit()
    assert field_jobs.list(db_session, FieldJobsQuery(visit.first_user)) == []
    assert field_jobs.list(db_session, FieldJobsQuery(visit.second_user)) == []


def test_stale_generation_cannot_reselect(db_session: Session, visit: Fixture) -> None:
    first = work_order_commands.assign(
        db_session, command=command(visit), context=context(visit)
    )
    with pytest.raises(WorkOrderCommandError, match="generation"):
        work_order_commands.assign(
            db_session,
            command=replace(
                command(visit),
                expected_revision=first.revision,
                expected_assignment_generation=uuid4(),
            ),
            context=context(visit),
        )


def test_technician_assignment_clears_selected_vendor_person(
    db_session: Session, visit: Fixture
) -> None:
    from app.models.dispatch import TechnicianProfile
    from app.services.work_order_assignment_contracts import TechnicianAssignmentTarget

    first = work_order_commands.assign(
        db_session, command=command(visit), context=context(visit)
    )
    technician = TechnicianProfile(person_id=uuid4(), is_active=True)
    db_session.add(technician)
    db_session.flush()
    technician_id = technician.id
    db_session.commit()
    result = work_order_commands.assign(
        db_session,
        command=replace(
            command(visit),
            target=TechnicianAssignmentTarget(technician_id),
            expected_revision=first.revision,
            expected_assignment_generation=first.assignment_generation,
        ),
        context=context(visit),
    )
    queue = db_session.get(WorkOrderAssignmentQueue, result.queue_id)
    assert queue.assigned_vendor_user_id is None and queue.assigned_vendor_id is None
    assert queue.assigned_technician_id == technician_id


def test_old_receipt_decodes_as_explicit_unselected_history(
    db_session: Session, visit: Fixture
) -> None:
    from app.models.dispatch import WorkOrderAssignmentReceipt
    from app.services.work_order_commands import _fingerprint

    ctx = context(visit)
    historical = replace(
        command(visit),
        target=VendorAssignmentTarget(visit.vendor_id),
        expected_revision=None,
    )
    db_session.add(
        WorkOrderAssignmentReceipt(
            command_id=ctx.command_id,
            idempotency_key=ctx.idempotency_key,
            fingerprint=_fingerprint(
                {
                    "work_order": visit.public_id,
                    "target_kind": "VendorAssignmentTarget",
                    "target_id": str(visit.vendor_id),
                    "start": None,
                    "end": None,
                    "status": "dispatched",
                    "reason": None,
                    "rule": None,
                    "expected_revision": None,
                }
            ),
            outcome={
                "queue_id": str(uuid4()),
                "work_order_id": visit.public_id,
                "vendor_id": str(visit.vendor_id),
                "technician_id": None,
                "status": "dispatched",
                "revision": visit.revision.isoformat(),
            },
        )
    )
    db_session.commit()
    replay = work_order_commands.assign(db_session, command=historical, context=ctx)
    assert replay.replayed and replay.target == VendorAssignmentTarget(
        visit.vendor_id, None
    )
    assert replay.assignment_generation is None
    with pytest.raises(WorkOrderCommandError, match="permission admission"):
        work_order_commands.assign(
            db_session,
            command=replace(
                historical,
                access=PermissionAdmission(
                    visit.staff_user, "system_user", frozenset()
                ),
            ),
            context=ctx,
        )


def test_queue_adapters_share_selected_person_owner(
    db_session: Session, visit: Fixture
) -> None:
    from app.schemas.dispatch import (
        WorkOrderAssignmentQueueCreate,
        WorkOrderAssignmentQueueUpdate,
    )

    access = command(visit).access
    job = db_session.query(WorkOrder).filter_by(public_id=visit.public_id).one()
    job_id = job.id
    db_session.commit()
    first = work_order_commands.create_queue_entry(
        db_session,
        WorkOrderAssignmentQueueCreate(
            work_order_mirror_id=job_id,
            status="assigned",
            assigned_vendor_id=visit.vendor_id,
            assigned_vendor_user_id=visit.first_member,
            expected_revision=visit.revision,
        ),
        context=context(visit),
        access=access,
    )
    row = db_session.query(WorkOrder).filter_by(public_id=visit.public_id).one()
    queue = db_session.get(WorkOrderAssignmentQueue, first.id)
    revision, generation = row.updated_at, queue.assignment_generation
    db_session.commit()
    second = work_order_commands.update_queue_entry(
        db_session,
        str(first.id),
        WorkOrderAssignmentQueueUpdate(
            assigned_vendor_user_id=visit.second_member,
            expected_revision=revision,
            expected_assignment_generation=generation,
        ),
        context=context(visit),
        access=access,
    )
    assert second.assigned_vendor_user_id == visit.second_member
    assert second.assigned_technician_id is None


def test_dispatcher_deactivation_revokes_receipt_replay(
    db_session: Session, visit: Fixture
) -> None:
    ctx = context(visit)
    cmd = command(visit)
    work_order_commands.assign(db_session, command=cmd, context=ctx)
    db_session.get(SystemUser, visit.staff_user).is_active = False
    db_session.commit()
    with pytest.raises(WorkOrderCommandError, match="active staff"):
        work_order_commands.assign(db_session, command=cmd, context=ctx)
