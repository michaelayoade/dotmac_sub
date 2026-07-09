from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.dispatch import (
    AvailabilityBlock,
    DispatchQueueStatus,
    DispatchRule,
    Shift,
    Skill,
    TechnicianProfile,
    TechnicianSkill,
    WorkOrderAssignmentQueue,
)
from app.models.subscriber import Subscriber, UserType
from app.models.system_user import SystemUser
from app.models.work_order_mirror import WorkOrderMirror


def _system_user(db_session) -> SystemUser:
    user = SystemUser(
        first_name="Ade",
        last_name="Tech",
        display_name="Ade Tech",
        email=f"ade-{uuid4().hex[:8]}@example.com",
        phone="+2348000000000",
        user_type=UserType.system_user,
        is_active=True,
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


def test_technician_profile_links_native_user_and_crm_person(db_session):
    user = _system_user(db_session)
    profile = TechnicianProfile(
        person_id=user.id,
        system_user_id=user.id,
        crm_person_id="crm-person-1",
        title="Field Technician",
        region="Jabi",
        erp_employee_id="EMP-1",
        metadata_={"source": "crm"},
    )
    skill = Skill(name="fiber_splicing", description="Fiber splicing")
    db_session.add_all([profile, skill])
    db_session.flush()
    db_session.add(
        TechnicianSkill(
            technician_id=profile.id,
            skill_id=skill.id,
            proficiency=4,
            is_primary=True,
        )
    )
    db_session.commit()

    loaded = db_session.get(TechnicianProfile, profile.id)
    assert loaded.system_user.email == user.email
    assert loaded.person_id == user.id
    assert loaded.crm_person_id == "crm-person-1"
    assert loaded.skills[0].skill.name == "fiber_splicing"


def test_skill_name_and_technician_skill_are_unique(db_session):
    user = _system_user(db_session)
    profile = TechnicianProfile(person_id=user.id, system_user_id=user.id)
    skill = Skill(name="installation")
    db_session.add_all([profile, skill])
    db_session.commit()

    db_session.add(Skill(name="installation"))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    user = _system_user(db_session)
    profile = TechnicianProfile(person_id=user.id, system_user_id=user.id)
    skill = Skill(name="activation")
    db_session.add_all([profile, skill])
    db_session.commit()
    profile_id = profile.id
    skill_id = skill.id
    db_session.add(
        TechnicianSkill(technician_id=profile_id, skill_id=skill_id, is_primary=True)
    )
    db_session.commit()
    db_session.add(TechnicianSkill(technician_id=profile_id, skill_id=skill_id))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_shift_availability_and_assignment_queue_reference_mirror(db_session):
    user = _system_user(db_session)
    subscriber = _subscriber(db_session)
    technician = TechnicianProfile(person_id=user.id, system_user_id=user.id)
    work_order = WorkOrderMirror(
        crm_work_order_id="wo-dispatch-1",
        subscriber_id=subscriber.id,
        title="Fibre install",
        status="scheduled",
    )
    db_session.add_all([technician, work_order])
    db_session.flush()

    start = datetime.now(UTC)
    rule = DispatchRule(name="Install dispatch", work_type="install", priority=10)
    shift = Shift(
        technician_id=technician.id,
        start_at=start,
        end_at=start + timedelta(hours=8),
        timezone="Africa/Lagos",
    )
    block = AvailabilityBlock(
        technician_id=technician.id,
        start_at=start + timedelta(hours=4),
        end_at=start + timedelta(hours=5),
        reason="Inventory pickup",
    )
    queued = WorkOrderAssignmentQueue(
        work_order_mirror_id=work_order.id,
        crm_work_order_id=work_order.crm_work_order_id,
        dispatch_rule_id=rule.id,
        assigned_technician_id=technician.id,
    )
    db_session.add_all([rule, shift, block, queued])
    db_session.commit()

    loaded = db_session.get(WorkOrderAssignmentQueue, queued.id)
    assert loaded.status == DispatchQueueStatus.queued
    assert loaded.work_order.crm_work_order_id == "wo-dispatch-1"
    assert loaded.assigned_technician.system_user_id == user.id
    assert technician.shifts[0].timezone == "Africa/Lagos"
    assert technician.availability_blocks[0].reason == "Inventory pickup"
