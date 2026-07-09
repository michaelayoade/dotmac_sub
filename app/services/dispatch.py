from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.dispatch import (
    AvailabilityBlock,
    DispatchRule,
    Shift,
    Skill,
    TechnicianProfile,
    TechnicianSkill,
    WorkOrderAssignmentQueue,
)
from app.models.service_team import ServiceTeam
from app.models.system_user import SystemUser
from app.models.work_order_mirror import WorkOrderMirror
from app.schemas.dispatch import (
    AvailabilityBlockCreate,
    AvailabilityBlockUpdate,
    DispatchRuleCreate,
    DispatchRuleUpdate,
    ShiftCreate,
    ShiftUpdate,
    SkillCreate,
    SkillUpdate,
    TechnicianProfileCreate,
    TechnicianProfileUpdate,
    TechnicianSkillCreate,
    TechnicianSkillUpdate,
    WorkOrderAssignmentQueueCreate,
    WorkOrderAssignmentQueueUpdate,
)
from app.services.common import apply_ordering, apply_pagination, coerce_uuid
from app.services.response import ListResponseMixin


def _data(payload: Any, *, exclude_unset: bool = False) -> dict[str, Any]:
    if hasattr(payload, "model_dump"):
        return payload.model_dump(exclude_unset=exclude_unset)
    return dict(payload)


def _get_or_404(db: Session, model, value, detail: str):
    entity = db.get(model, coerce_uuid(value))
    if not entity:
        raise HTTPException(status_code=404, detail=detail)
    return entity


def _ensure_system_user(db: Session, system_user_id) -> None:
    if system_user_id is not None:
        _get_or_404(db, SystemUser, system_user_id, "System user not found")


def _ensure_technician(db: Session, technician_id) -> TechnicianProfile:
    return _get_or_404(db, TechnicianProfile, technician_id, "Technician not found")


def _ensure_skill(db: Session, skill_id) -> Skill:
    return _get_or_404(db, Skill, skill_id, "Skill not found")


def _ensure_service_team(db: Session, service_team_id) -> None:
    if service_team_id is not None:
        _get_or_404(db, ServiceTeam, service_team_id, "Service team not found")


def _ensure_rule(db: Session, rule_id) -> None:
    if rule_id is not None:
        _get_or_404(db, DispatchRule, rule_id, "Dispatch rule not found")


def _normalize_skill_ids(db: Session, data: dict[str, Any]) -> None:
    if "skill_ids" not in data:
        return
    normalized: list[str] = []
    for skill_id in data.get("skill_ids") or []:
        skill = _ensure_skill(db, skill_id)
        normalized.append(str(skill.id))
    data["skill_ids"] = normalized


def _resolve_work_order(db: Session, payload: WorkOrderAssignmentQueueCreate) -> WorkOrderMirror:
    if payload.work_order_mirror_id is not None:
        return _get_or_404(
            db,
            WorkOrderMirror,
            payload.work_order_mirror_id,
            "Work order mirror not found",
        )
    row = (
        db.query(WorkOrderMirror)
        .filter(WorkOrderMirror.crm_work_order_id == payload.crm_work_order_id)
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Work order mirror not found")
    return row


class Skills(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: SkillCreate) -> Skill:
        skill = Skill(**_data(payload))
        db.add(skill)
        db.commit()
        db.refresh(skill)
        return skill

    @staticmethod
    def get(db: Session, skill_id: str) -> Skill:
        return _ensure_skill(db, skill_id)

    @staticmethod
    def list(
        db: Session,
        *,
        is_active: bool | None = True,
        order_by: str = "name",
        order_dir: str = "asc",
        limit: int = 50,
        offset: int = 0,
    ) -> list[Skill]:
        query = db.query(Skill)
        if is_active is not None:
            query = query.filter(Skill.is_active.is_(is_active))
        query = apply_ordering(
            query, order_by, order_dir, {"name": Skill.name, "created_at": Skill.created_at}
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, skill_id: str, payload: SkillUpdate) -> Skill:
        skill = Skills.get(db, skill_id)
        for key, value in _data(payload, exclude_unset=True).items():
            setattr(skill, key, value)
        db.commit()
        db.refresh(skill)
        return skill

    @staticmethod
    def delete(db: Session, skill_id: str) -> None:
        skill = Skills.get(db, skill_id)
        skill.is_active = False
        db.commit()


class TechnicianProfiles(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: TechnicianProfileCreate) -> TechnicianProfile:
        data = _data(payload)
        _ensure_system_user(db, data.get("system_user_id"))
        profile = TechnicianProfile(**data)
        db.add(profile)
        db.commit()
        db.refresh(profile)
        return profile

    @staticmethod
    def get(db: Session, technician_id: str) -> TechnicianProfile:
        return _ensure_technician(db, technician_id)

    @staticmethod
    def list(
        db: Session,
        *,
        region: str | None = None,
        is_active: bool | None = True,
        order_by: str = "created_at",
        order_dir: str = "desc",
        limit: int = 50,
        offset: int = 0,
    ) -> list[TechnicianProfile]:
        query = db.query(TechnicianProfile)
        if region:
            query = query.filter(TechnicianProfile.region == region)
        if is_active is not None:
            query = query.filter(TechnicianProfile.is_active.is_(is_active))
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": TechnicianProfile.created_at,
                "region": TechnicianProfile.region,
                "title": TechnicianProfile.title,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(
        db: Session, technician_id: str, payload: TechnicianProfileUpdate
    ) -> TechnicianProfile:
        profile = TechnicianProfiles.get(db, technician_id)
        data = _data(payload, exclude_unset=True)
        _ensure_system_user(db, data.get("system_user_id"))
        if data.get("person_id") is None and data.get("system_user_id") is not None:
            data["person_id"] = data["system_user_id"]
        for key, value in data.items():
            setattr(profile, key, value)
        db.commit()
        db.refresh(profile)
        return profile

    @staticmethod
    def delete(db: Session, technician_id: str) -> None:
        profile = TechnicianProfiles.get(db, technician_id)
        profile.is_active = False
        db.commit()


class TechnicianSkills(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: TechnicianSkillCreate) -> TechnicianSkill:
        _ensure_technician(db, payload.technician_id)
        _ensure_skill(db, payload.skill_id)
        row = TechnicianSkill(**_data(payload))
        db.add(row)
        db.commit()
        db.refresh(row)
        return row

    @staticmethod
    def list(
        db: Session,
        *,
        technician_id: str | None = None,
        skill_id: str | None = None,
        is_active: bool | None = True,
        limit: int = 50,
        offset: int = 0,
    ) -> list[TechnicianSkill]:
        query = db.query(TechnicianSkill)
        if technician_id:
            query = query.filter(TechnicianSkill.technician_id == coerce_uuid(technician_id))
        if skill_id:
            query = query.filter(TechnicianSkill.skill_id == coerce_uuid(skill_id))
        if is_active is not None:
            query = query.filter(TechnicianSkill.is_active.is_(is_active))
        query = query.order_by(TechnicianSkill.created_at.desc())
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, row_id: str, payload: TechnicianSkillUpdate) -> TechnicianSkill:
        row = _get_or_404(db, TechnicianSkill, row_id, "Technician skill not found")
        for key, value in _data(payload, exclude_unset=True).items():
            setattr(row, key, value)
        db.commit()
        db.refresh(row)
        return row


class Shifts(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: ShiftCreate) -> Shift:
        _ensure_technician(db, payload.technician_id)
        row = Shift(**_data(payload))
        db.add(row)
        db.commit()
        db.refresh(row)
        return row

    @staticmethod
    def list(
        db: Session,
        *,
        technician_id: str | None = None,
        is_active: bool | None = True,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Shift]:
        query = db.query(Shift)
        if technician_id:
            query = query.filter(Shift.technician_id == coerce_uuid(technician_id))
        if is_active is not None:
            query = query.filter(Shift.is_active.is_(is_active))
        query = query.order_by(Shift.start_at.asc())
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, shift_id: str, payload: ShiftUpdate) -> Shift:
        row = _get_or_404(db, Shift, shift_id, "Shift not found")
        data = _data(payload, exclude_unset=True)
        start_at = data.get("start_at", row.start_at)
        end_at = data.get("end_at", row.end_at)
        if start_at is not None and end_at is not None and end_at <= start_at:
            raise HTTPException(status_code=400, detail="end_at must be after start_at")
        for key, value in data.items():
            setattr(row, key, value)
        db.commit()
        db.refresh(row)
        return row


class AvailabilityBlocks(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: AvailabilityBlockCreate) -> AvailabilityBlock:
        _ensure_technician(db, payload.technician_id)
        row = AvailabilityBlock(**_data(payload))
        db.add(row)
        db.commit()
        db.refresh(row)
        return row

    @staticmethod
    def list(
        db: Session,
        *,
        technician_id: str | None = None,
        is_active: bool | None = True,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AvailabilityBlock]:
        query = db.query(AvailabilityBlock)
        if technician_id:
            query = query.filter(AvailabilityBlock.technician_id == coerce_uuid(technician_id))
        if is_active is not None:
            query = query.filter(AvailabilityBlock.is_active.is_(is_active))
        query = query.order_by(AvailabilityBlock.start_at.asc())
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(
        db: Session, block_id: str, payload: AvailabilityBlockUpdate
    ) -> AvailabilityBlock:
        row = _get_or_404(db, AvailabilityBlock, block_id, "Availability block not found")
        data = _data(payload, exclude_unset=True)
        start_at = data.get("start_at", row.start_at)
        end_at = data.get("end_at", row.end_at)
        if start_at is not None and end_at is not None and end_at <= start_at:
            raise HTTPException(status_code=400, detail="end_at must be after start_at")
        for key, value in data.items():
            setattr(row, key, value)
        db.commit()
        db.refresh(row)
        return row


class DispatchRules(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: DispatchRuleCreate) -> DispatchRule:
        data = _data(payload)
        _ensure_service_team(db, data.get("service_team_id"))
        _normalize_skill_ids(db, data)
        row = DispatchRule(**data)
        db.add(row)
        db.commit()
        db.refresh(row)
        return row

    @staticmethod
    def list(
        db: Session,
        *,
        work_type: str | None = None,
        region: str | None = None,
        is_active: bool | None = True,
        limit: int = 50,
        offset: int = 0,
    ) -> list[DispatchRule]:
        query = db.query(DispatchRule)
        if work_type:
            query = query.filter(DispatchRule.work_type == work_type)
        if region:
            query = query.filter(DispatchRule.region == region)
        if is_active is not None:
            query = query.filter(DispatchRule.is_active.is_(is_active))
        query = query.order_by(DispatchRule.priority.desc(), DispatchRule.created_at.desc())
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, rule_id: str, payload: DispatchRuleUpdate) -> DispatchRule:
        row = _get_or_404(db, DispatchRule, rule_id, "Dispatch rule not found")
        data = _data(payload, exclude_unset=True)
        _ensure_service_team(db, data.get("service_team_id"))
        _normalize_skill_ids(db, data)
        for key, value in data.items():
            setattr(row, key, value)
        db.commit()
        db.refresh(row)
        return row


class AssignmentQueue(ListResponseMixin):
    @staticmethod
    def create(
        db: Session, payload: WorkOrderAssignmentQueueCreate
    ) -> WorkOrderAssignmentQueue:
        work_order = _resolve_work_order(db, payload)
        data = _data(payload)
        data["work_order_mirror_id"] = work_order.id
        data["crm_work_order_id"] = work_order.crm_work_order_id
        _ensure_rule(db, data.get("dispatch_rule_id"))
        if data.get("assigned_technician_id") is not None:
            _ensure_technician(db, data["assigned_technician_id"])
        row = WorkOrderAssignmentQueue(**data)
        db.add(row)
        db.commit()
        db.refresh(row)
        return row

    @staticmethod
    def list(
        db: Session,
        *,
        status: str | None = None,
        crm_work_order_id: str | None = None,
        assigned_technician_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[WorkOrderAssignmentQueue]:
        query = db.query(WorkOrderAssignmentQueue)
        if status:
            query = query.filter(WorkOrderAssignmentQueue.status == status)
        if crm_work_order_id:
            query = query.filter(WorkOrderAssignmentQueue.crm_work_order_id == crm_work_order_id)
        if assigned_technician_id:
            query = query.filter(
                WorkOrderAssignmentQueue.assigned_technician_id
                == coerce_uuid(assigned_technician_id)
            )
        query = query.order_by(WorkOrderAssignmentQueue.created_at.desc())
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(
        db: Session, queue_id: str, payload: WorkOrderAssignmentQueueUpdate
    ) -> WorkOrderAssignmentQueue:
        row = _get_or_404(db, WorkOrderAssignmentQueue, queue_id, "Queue item not found")
        data = _data(payload, exclude_unset=True)
        _ensure_rule(db, data.get("dispatch_rule_id"))
        if data.get("assigned_technician_id") is not None:
            _ensure_technician(db, data["assigned_technician_id"])
        for key, value in data.items():
            setattr(row, key, value)
        db.commit()
        db.refresh(row)
        return row


skills = Skills()
technician_profiles = TechnicianProfiles()
technician_skills = TechnicianSkills()
shifts = Shifts()
availability_blocks = AvailabilityBlocks()
dispatch_rules = DispatchRules()
assignment_queue = AssignmentQueue()
