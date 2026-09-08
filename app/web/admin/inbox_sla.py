"""Administrator API for Inbox SLA policy configuration."""

from __future__ import annotations

from datetime import date, time
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.inbox_sla import InboxSlaPolicy
from app.services.auth_dependencies import require_permission
from app.services.db_session_adapter import db_session_adapter
from app.services.inbox_sla import SlaPolicyInput, SlaRuleInput, create_policy

router = APIRouter(prefix="/inbox/sla", tags=["admin-inbox-sla"])


class SlaRuleRequest(BaseModel):
    first_response_minutes: int = Field(gt=0)
    resolution_minutes: int = Field(gt=0)
    warning_minutes: int = Field(default=0, ge=0)
    next_response_minutes: int | None = Field(default=None, gt=0)
    service_team_id: UUID | None = None
    channel_type: str | None = None
    priority: int | None = None


class SlaPolicyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str | None = None
    rules: tuple[SlaRuleRequest, ...] = Field(min_length=1)
    timezone: str = "Africa/Lagos"
    working_days: tuple[int, ...] = (0, 1, 2, 3, 4)
    workday_start: time = time(9)
    workday_end: time = time(17)
    holidays: tuple[date, ...] = ()
    is_default: bool = False


def _serialize(policy: InboxSlaPolicy) -> dict[str, object]:
    return {
        "id": str(policy.id),
        "name": policy.name,
        "description": policy.description,
        "timezone": policy.timezone,
        "working_days": policy.working_days,
        "workday_start": policy.workday_start.isoformat(),
        "workday_end": policy.workday_end.isoformat(),
        "holidays": policy.holidays or [],
        "is_active": policy.is_active,
        "is_default": policy.is_default,
        "rules": [
            {
                "id": str(rule.id),
                "service_team_id": str(rule.service_team_id)
                if rule.service_team_id
                else None,
                "channel_type": rule.channel_type,
                "priority": rule.priority,
                "first_response_minutes": rule.first_response_minutes,
                "next_response_minutes": rule.next_response_minutes,
                "resolution_minutes": rule.resolution_minutes,
                "warning_minutes": rule.warning_minutes,
                "is_active": rule.is_active,
            }
            for rule in policy.rules
        ],
    }


@router.get(
    "/policies", dependencies=[Depends(require_permission("support:ticket:read"))]
)
def list_policies(db: Session = Depends(get_db)) -> list[dict[str, object]]:
    return [
        _serialize(policy)
        for policy in db.query(InboxSlaPolicy).order_by(InboxSlaPolicy.name).all()
    ]


@router.post(
    "/policies", dependencies=[Depends(require_permission("support:ticket:update"))]
)
def add_policy(request: SlaPolicyRequest) -> dict[str, object]:
    command = SlaPolicyInput(
        name=request.name,
        description=request.description,
        rules=tuple(SlaRuleInput(**rule.model_dump()) for rule in request.rules),
        timezone=request.timezone,
        working_days=request.working_days,
        workday_start=request.workday_start,
        workday_end=request.workday_end,
        holidays=request.holidays,
        is_default=request.is_default,
    )
    with db_session_adapter.session() as db:
        return _serialize(create_policy(db, command))


@router.get(
    "/policies/{policy_id}",
    dependencies=[Depends(require_permission("support:ticket:read"))],
)
def get_policy(policy_id: UUID, db: Session = Depends(get_db)) -> dict[str, object]:
    policy = db.get(InboxSlaPolicy, policy_id)
    if policy is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Inbox SLA policy not found")
    return _serialize(policy)


@router.post(
    "/policies/{policy_id}/active",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def set_policy_active(policy_id: UUID, active: bool) -> dict[str, object]:
    with db_session_adapter.session() as db:
        policy = db.get(InboxSlaPolicy, policy_id)
        if policy is None:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="Inbox SLA policy not found")
        policy.is_active = active
        if not active:
            policy.is_default = False
        db.flush()
        return _serialize(policy)
