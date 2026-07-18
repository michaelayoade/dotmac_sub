from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.audit import AuditActorType, AuditEvent
from app.models.dispatch import TechnicianProfile, WorkOrderAssignmentQueue
from app.models.subscriber import Subscriber, UserType
from app.models.system_user import SystemUser
from app.schemas.dispatch import (
    WorkOrderAssignmentQueueUpdate,
    WorkOrderHeaderCreate,
    WorkOrderHeaderUpdate,
)
from app.services.work_order_commands import work_order_commands


def _subscriber(db_session) -> Subscriber:
    row = Subscriber(
        first_name="Command",
        last_name="Customer",
        email=f"work-order-command-{uuid4().hex[:8]}@example.com",
    )
    db_session.add(row)
    db_session.flush()
    return row


def _technician(db_session) -> TechnicianProfile:
    user = SystemUser(
        first_name="Ada",
        last_name="Technician",
        display_name="Ada Technician",
        email=f"work-order-tech-{uuid4().hex[:8]}@example.com",
        user_type=UserType.system_user,
    )
    db_session.add(user)
    db_session.flush()
    row = TechnicianProfile(
        person_id=user.id,
        system_user_id=user.id,
        crm_person_id=f"crm-{uuid4().hex[:8]}",
        is_active=True,
    )
    db_session.add(row)
    db_session.flush()
    return row


def test_native_create_replays_same_public_id_and_audits_once(db_session):
    subscriber = _subscriber(db_session)
    payload = WorkOrderHeaderCreate(
        public_id="sub-command-create",
        subscriber_id=subscriber.id,
        title="Validate FAT",
        status="scheduled",
        metadata={"fiber_field_verification_plan": {"forged": True}},
    )

    created = work_order_commands.create(db_session, payload)
    replayed = work_order_commands.create(db_session, payload)

    assert replayed.id == created.id
    assert "fiber_field_verification_plan" not in created.metadata_
    events = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "work_order.created")
        .filter(AuditEvent.entity_id == created.public_id)
        .all()
    )
    assert len(events) == 1
    assert events[0].metadata_["owner"] == "operations.work_order_commands"


def test_assignment_preview_is_read_only_and_assignment_is_atomic_replay(
    db_session,
):
    subscriber = _subscriber(db_session)
    technician = _technician(db_session)
    work_order = work_order_commands.create(
        db_session,
        WorkOrderHeaderCreate(
            public_id="sub-command-assign",
            subscriber_id=subscriber.id,
            title="Trace feeder",
            status="scheduled",
        ),
    )
    auth = {
        "principal_type": "system_user",
        "principal_id": str(uuid4()),
    }

    preview = work_order_commands.preview_assignment(
        db_session,
        work_order.public_id,
        technician_id=technician.id,
    )
    assert preview["previous"]["status"] == "scheduled"
    assert preview["result"]["status"] == "dispatched"
    assert (
        db_session.query(WorkOrderAssignmentQueue)
        .filter(WorkOrderAssignmentQueue.work_order_mirror_id == work_order.id)
        .count()
        == 0
    )

    assigned = work_order_commands.assign(
        db_session,
        work_order.public_id,
        technician_id=technician.id,
        reason="field_verification",
        auth=auth,
        request_id="assignment-command-1",
    )
    replayed = work_order_commands.assign(
        db_session,
        work_order.public_id,
        technician_id=technician.id,
        reason="field_verification",
        auth=auth,
        request_id="assignment-command-1",
    )

    assert replayed.id == assigned.id
    db_session.refresh(work_order)
    assert work_order.status == "dispatched"
    assert work_order.assigned_to_name == "Ada Technician"
    assert assigned.status == "assigned"
    assert assigned.assigned_technician_id == technician.id
    events = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "work_order.assigned")
        .filter(AuditEvent.entity_id == work_order.public_id)
        .all()
    )
    assert len(events) == 1
    assert events[0].actor_type == AuditActorType.user
    assert events[0].request_id == "assignment-command-1"
    assert events[0].metadata_["queue_id"] == str(assigned.id)
    assert events[0].metadata_["previous"]["status"] == "scheduled"
    assert events[0].metadata_["result"]["status"] == "dispatched"
    assert events[0].metadata_["result"]["technician_id"] == str(technician.id)

    skipped = work_order_commands.update_queue_entry(
        db_session,
        str(assigned.id),
        WorkOrderAssignmentQueueUpdate(status="skipped"),
        auth=auth,
        request_id="assignment-command-2",
    )
    db_session.refresh(work_order)
    assert skipped.status == "skipped"
    assert work_order.status == "scheduled"
    assert work_order.assigned_to_crm_person_id is None
    assert work_order.assigned_to_name is None
    transition = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "work_order.assignment_queue_transitioned")
        .filter(AuditEvent.entity_id == work_order.public_id)
        .one()
    )
    assert transition.metadata_["work_order_projection"]["previous"]["status"] == (
        "dispatched"
    )
    assert transition.metadata_["work_order_projection"]["result"]["status"] == (
        "scheduled"
    )


def test_header_command_rejects_parallel_assignment_and_field_status(db_session):
    subscriber = _subscriber(db_session)
    work_order = work_order_commands.create(
        db_session,
        WorkOrderHeaderCreate(
            public_id="sub-command-guard",
            subscriber_id=subscriber.id,
            title="Guard owner",
            status="scheduled",
        ),
    )

    with pytest.raises(HTTPException) as assignment:
        work_order_commands.update_header(
            db_session,
            work_order.public_id,
            WorkOrderHeaderUpdate(assigned_to_name="Bypass"),
        )
    assert assignment.value.status_code == 422

    with pytest.raises(HTTPException) as transition:
        work_order_commands.update_header(
            db_session,
            work_order.public_id,
            WorkOrderHeaderUpdate(status="completed"),
        )
    assert transition.value.status_code == 422
    assert "field transition owner" in transition.value.detail
