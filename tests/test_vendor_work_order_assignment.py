"""Fast unit-lane checks; PostgreSQL migration acceptance is a separate gate."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import CheckConstraint, Index

from app.models.dispatch import TechnicianProfile, WorkOrderAssignmentQueue
from app.models.field_job_event import FieldJobEvent
from app.models.field_movement import FieldWorkOrderMovement
from app.models.field_note import FieldWorkOrderNote
from app.models.field_worklog import FieldWorkLog
from app.models.subscriber import Subscriber
from app.models.vendor_routes import Vendor
from app.models.work_order import WorkOrder
from app.services.field.manager import field_manager
from app.services.owner_commands import CommandContext
from app.services.work_order_assignment_contracts import (
    TechnicianAssignmentTarget,
    VendorAssignmentTarget,
    WorkOrderAssignmentCommand,
    WorkOrderAssignmentQuery,
)
from app.services.work_order_commands import work_order_commands
from app.services.work_order_errors import WorkOrderCommandError


def _fixture(db_session, *, lifecycle_status="in_progress"):
    vendor = Vendor(name="Native vendor", is_active=True)
    technician = TechnicianProfile(
        person_id=uuid4(), title="Staff technician", is_active=True
    )
    subscriber = Subscriber(
        first_name="Assignment",
        last_name="Customer",
        email=f"assignment-{uuid4()}@example.com",
    )
    db_session.add(subscriber)
    db_session.flush()
    row = WorkOrder(
        subscriber_id=subscriber.id,
        public_id=f"assignment-{uuid4()}",
        title="Restore service",
        status=lifecycle_status,
        is_active=True,
    )
    db_session.add_all([vendor, technician, row])
    db_session.commit()
    ids = (row.public_id, vendor.id, technician.id)
    db_session.commit()
    return ids


def _context():
    return CommandContext.system(
        actor=str(uuid4()),
        scope="work_order:assignment",
        reason="Dispatch repair",
        idempotency_key=uuid4().hex,
    )


@pytest.mark.parametrize("lifecycle_status", ["in_progress", "paused"])
@pytest.mark.parametrize("requested_status", ["scheduled", "dispatched"])
def test_vendor_reassignment_preserves_execution_and_revokes_opposing_target(
    db_session,
    lifecycle_status,
    requested_status,
):
    public_id, vendor_id, technician_id = _fixture(
        db_session, lifecycle_status=lifecycle_status
    )
    first = work_order_commands.assign(
        db_session,
        command=WorkOrderAssignmentCommand(
            work_order_public_id=public_id,
            target=TechnicianAssignmentTarget(technician_id),
            status=requested_status,
        ),
        context=_context(),
    )
    assert first.status == lifecycle_status
    second = work_order_commands.assign(
        db_session,
        command=WorkOrderAssignmentCommand(
            work_order_public_id=public_id,
            target=VendorAssignmentTarget(vendor_id),
            status=requested_status,
        ),
        context=_context(),
    )
    assert first.queue_id == second.queue_id
    row = db_session.query(WorkOrder).filter_by(public_id=public_id).one()
    queue = db_session.get(WorkOrderAssignmentQueue, second.queue_id)
    assert row.status == lifecycle_status
    assert row.technician_name is None
    assert row.technician_phone is None
    assert queue.assigned_technician_id is None
    assert queue.assigned_vendor_id == vendor_id
    assert (
        db_session.query(WorkOrderAssignmentQueue)
        .filter_by(work_order_mirror_id=row.id, status="assigned")
        .count()
        == 1
    )


def test_assignment_replay_is_immutable_and_content_conflicts_fail_closed(db_session):
    public_id, vendor_id, technician_id = _fixture(db_session)
    context = _context()
    command = WorkOrderAssignmentCommand(
        work_order_public_id=public_id, target=VendorAssignmentTarget(vendor_id)
    )
    first = work_order_commands.assign(db_session, command=command, context=context)
    replay = work_order_commands.assign(db_session, command=command, context=context)
    assert replay.replayed and replay.queue_id == first.queue_id
    with pytest.raises(WorkOrderCommandError, match="different content"):
        work_order_commands.assign(
            db_session,
            command=WorkOrderAssignmentCommand(
                work_order_public_id=public_id,
                target=TechnicianAssignmentTarget(technician_id),
            ),
            context=context,
        )


def test_preview_revision_rejects_stale_assignment(db_session):
    public_id, vendor_id, technician_id = _fixture(db_session)
    preview = work_order_commands.preview_assignment(
        db_session,
        query=WorkOrderAssignmentQuery(
            work_order_public_id=public_id, target=VendorAssignmentTarget(vendor_id)
        ),
    )
    db_session.commit()
    work_order_commands.assign(
        db_session,
        command=WorkOrderAssignmentCommand(
            work_order_public_id=public_id,
            target=TechnicianAssignmentTarget(technician_id),
        ),
        context=_context(),
    )
    with pytest.raises(WorkOrderCommandError, match="changed since"):
        work_order_commands.assign(
            db_session,
            command=WorkOrderAssignmentCommand(
                work_order_public_id=public_id,
                target=VendorAssignmentTarget(vendor_id),
                expected_revision=preview.revision,
            ),
            context=_context(),
        )


def test_vendor_actor_columns_are_explicit_and_not_fabricated_person_ids():
    for model, column, person in (
        (FieldJobEvent, "author_vendor_user_id", "person_id"),
        (FieldWorkLog, "author_vendor_user_id", "person_id"),
        (FieldWorkOrderNote, "author_vendor_user_id", "author_person_id"),
        (FieldWorkOrderMovement, "actor_vendor_user_id", "actor_person_id"),
    ):
        assert model.__table__.c[person].nullable
        assert {
            fk.target_fullname for fk in model.__table__.c[column].foreign_keys
        } == {"field_vendor_users.id"}
        checks = [
            str(c.sqltext)
            for c in model.__table__.constraints
            if isinstance(c, CheckConstraint)
        ]
        assert any(
            f"{column} IS NOT NULL" in c and f"{person} IS NULL" in c for c in checks
        )
    indexes = [
        i for i in WorkOrderAssignmentQueue.__table__.indexes if isinstance(i, Index)
    ]
    assert any(
        i.name == "uq_work_order_current_assignment" and i.unique for i in indexes
    )


def test_vendor_assignment_counts_as_assigned_on_manager_summary(db_session):
    public_id, vendor_id, _technician_id = _fixture(db_session)
    before = field_manager.summary(db_session)
    db_session.commit()
    work_order_commands.assign(
        db_session,
        command=WorkOrderAssignmentCommand(
            work_order_public_id=public_id, target=VendorAssignmentTarget(vendor_id)
        ),
        context=_context(),
    )
    after = field_manager.summary(db_session)
    assert after["open_jobs"] == before["open_jobs"]
    assert after["unassigned_jobs"] == before["unassigned_jobs"] - 1
