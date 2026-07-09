from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.models.dispatch import DispatchQueueStatus, TechnicianProfile
from app.models.service_team import ServiceTeam, ServiceTeamType
from app.models.subscriber import Subscriber, UserType
from app.models.system_user import SystemUser
from app.models.work_order_mirror import WorkOrderMirror
from app.schemas.dispatch import (
    AvailabilityBlockCreate,
    DispatchRuleCreate,
    ShiftCreate,
    SkillCreate,
    TechnicianProfileCreate,
    TechnicianSkillCreate,
    WorkOrderAssignmentQueueCreate,
    WorkOrderAssignmentQueueUpdate,
)
from app.services import dispatch


def _system_user(db_session) -> SystemUser:
    user = SystemUser(
        first_name="Ade",
        last_name="Tech",
        email=f"ade-{uuid4().hex[:8]}@example.com",
        user_type=UserType.system_user,
    )
    db_session.add(user)
    db_session.flush()
    return user


def _subscriber(db_session) -> Subscriber:
    sub = Subscriber(
        first_name="Adaeze",
        last_name="Nwosu",
        email=f"adaeze-{uuid4().hex[:8]}@example.com",
    )
    db_session.add(sub)
    db_session.flush()
    return sub


def _work_order(db_session) -> WorkOrderMirror:
    sub = _subscriber(db_session)
    row = WorkOrderMirror(
        crm_work_order_id="wo-service-1",
        subscriber_id=sub.id,
        title="Fibre install",
        status="scheduled",
    )
    db_session.add(row)
    db_session.flush()
    return row


def test_create_technician_profile_defaults_person_id_to_system_user(db_session):
    user = _system_user(db_session)

    profile = dispatch.technician_profiles.create(
        db_session,
        TechnicianProfileCreate(
            system_user_id=user.id,
            crm_person_id="crm-person-1",
            title="Field Tech",
            region="Jabi",
        ),
    )

    assert profile.person_id == user.id
    assert profile.system_user_id == user.id
    assert profile.crm_person_id == "crm-person-1"


def test_create_technician_profile_rejects_missing_identity():
    with pytest.raises(ValidationError):
        TechnicianProfileCreate(title="No identity")


def test_create_technician_profile_rejects_missing_system_user(db_session):
    with pytest.raises(HTTPException) as exc:
        dispatch.technician_profiles.create(
            db_session, TechnicianProfileCreate(system_user_id=uuid4())
        )
    assert exc.value.status_code == 404
    assert exc.value.detail == "System user not found"


def test_skill_technician_skill_and_rule_lifecycle(db_session):
    user = _system_user(db_session)
    team = ServiceTeam(name="Field Ops", team_type=ServiceTeamType.field_service.value)
    db_session.add(team)
    db_session.commit()

    profile = dispatch.technician_profiles.create(
        db_session, TechnicianProfileCreate(system_user_id=user.id)
    )
    skill = dispatch.skills.create(
        db_session, SkillCreate(name="fiber_splicing", description="Fiber splicing")
    )
    tech_skill = dispatch.technician_skills.create(
        db_session,
        TechnicianSkillCreate(
            technician_id=profile.id,
            skill_id=skill.id,
            proficiency=5,
            is_primary=True,
        ),
    )
    rule = dispatch.dispatch_rules.create(
        db_session,
        DispatchRuleCreate(
            name="Install routing",
            priority=20,
            work_type="install",
            service_team_id=team.id,
            skill_ids=[skill.id],
            auto_assign=False,
        ),
    )

    assert tech_skill.is_primary is True
    assert rule.skill_ids == [str(skill.id)]
    assert dispatch.skills.list(db_session)[0].name == "fiber_splicing"
    assert dispatch.dispatch_rules.list(db_session, work_type="install")[0].id == rule.id


def test_shift_and_availability_validate_time_windows(db_session):
    user = _system_user(db_session)
    profile = dispatch.technician_profiles.create(
        db_session, TechnicianProfileCreate(system_user_id=user.id)
    )
    start = datetime.now(UTC)

    shift = dispatch.shifts.create(
        db_session,
        ShiftCreate(
            technician_id=profile.id,
            start_at=start,
            end_at=start + timedelta(hours=8),
            timezone="Africa/Lagos",
        ),
    )
    block = dispatch.availability_blocks.create(
        db_session,
        AvailabilityBlockCreate(
            technician_id=profile.id,
            start_at=start + timedelta(hours=2),
            end_at=start + timedelta(hours=3),
            reason="Inventory pickup",
        ),
    )

    assert shift.timezone == "Africa/Lagos"
    assert block.reason == "Inventory pickup"
    with pytest.raises(ValidationError):
        ShiftCreate(technician_id=profile.id, start_at=start, end_at=start)


def test_assignment_queue_resolves_crm_work_order_and_updates_status(db_session):
    user = _system_user(db_session)
    profile = dispatch.technician_profiles.create(
        db_session, TechnicianProfileCreate(system_user_id=user.id)
    )
    work_order = _work_order(db_session)
    db_session.commit()

    queued = dispatch.assignment_queue.create(
        db_session,
        WorkOrderAssignmentQueueCreate(
            crm_work_order_id=work_order.crm_work_order_id,
            assigned_technician_id=profile.id,
            reason="Initial import",
        ),
    )
    assert queued.work_order_mirror_id == work_order.id
    assert queued.status == DispatchQueueStatus.queued

    updated = dispatch.assignment_queue.update(
        db_session,
        str(queued.id),
        WorkOrderAssignmentQueueUpdate(status="assigned"),
    )
    assert updated.status == "assigned"
    assert dispatch.assignment_queue.list(db_session, status="assigned")[0].id == queued.id


def test_assignment_queue_rejects_unknown_work_order(db_session):
    with pytest.raises(HTTPException) as exc:
        dispatch.assignment_queue.create(
            db_session,
            WorkOrderAssignmentQueueCreate(crm_work_order_id="missing"),
        )
    assert exc.value.status_code == 404
    assert exc.value.detail == "Work order mirror not found"


def test_delete_marks_skill_and_profile_inactive(db_session):
    user = _system_user(db_session)
    profile = dispatch.technician_profiles.create(
        db_session, TechnicianProfileCreate(system_user_id=user.id)
    )
    skill = dispatch.skills.create(db_session, SkillCreate(name="activation"))

    dispatch.skills.delete(db_session, str(skill.id))
    dispatch.technician_profiles.delete(db_session, str(profile.id))

    assert dispatch.skills.list(db_session) == []
    assert dispatch.technician_profiles.list(db_session) == []
    assert db_session.get(TechnicianProfile, profile.id).is_active is False
