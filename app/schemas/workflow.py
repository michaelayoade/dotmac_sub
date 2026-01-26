from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.workflow import SlaBreachStatus, SlaClockStatus, WorkflowEntityType


class StatusTransitionBase(BaseModel):
    from_status: str = Field(min_length=1, max_length=40)
    to_status: str = Field(min_length=1, max_length=40)
    requires_note: bool = False
    is_active: bool = True


class TicketStatusTransitionCreate(StatusTransitionBase):
    pass


class TicketStatusTransitionUpdate(BaseModel):
    from_status: str | None = Field(default=None, min_length=1, max_length=40)
    to_status: str | None = Field(default=None, min_length=1, max_length=40)
    requires_note: bool | None = None
    is_active: bool | None = None


class TicketStatusTransitionRead(StatusTransitionBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class WorkOrderStatusTransitionCreate(StatusTransitionBase):
    pass


class WorkOrderStatusTransitionUpdate(BaseModel):
    from_status: str | None = Field(default=None, min_length=1, max_length=40)
    to_status: str | None = Field(default=None, min_length=1, max_length=40)
    requires_note: bool | None = None
    is_active: bool | None = None


class WorkOrderStatusTransitionRead(StatusTransitionBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class ProjectTaskStatusTransitionCreate(StatusTransitionBase):
    pass


class ProjectTaskStatusTransitionUpdate(BaseModel):
    from_status: str | None = Field(default=None, min_length=1, max_length=40)
    to_status: str | None = Field(default=None, min_length=1, max_length=40)
    requires_note: bool | None = None
    is_active: bool | None = None


class ProjectTaskStatusTransitionRead(StatusTransitionBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class SlaPolicyBase(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    entity_type: WorkflowEntityType
    description: str | None = None
    is_active: bool = True


class SlaPolicyCreate(SlaPolicyBase):
    pass


class SlaPolicyUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    entity_type: WorkflowEntityType | None = None
    description: str | None = None
    is_active: bool | None = None


class SlaPolicyRead(SlaPolicyBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class SlaTargetBase(BaseModel):
    policy_id: UUID
    priority: str | None = Field(default=None, max_length=40)
    target_minutes: int = Field(ge=1)
    warning_minutes: int | None = Field(default=None, ge=1)
    is_active: bool = True


class SlaTargetCreate(SlaTargetBase):
    pass


class SlaTargetUpdate(BaseModel):
    policy_id: UUID | None = None
    priority: str | None = Field(default=None, max_length=40)
    target_minutes: int | None = Field(default=None, ge=1)
    warning_minutes: int | None = Field(default=None, ge=1)
    is_active: bool | None = None


class SlaTargetRead(SlaTargetBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class SlaClockBase(BaseModel):
    policy_id: UUID
    entity_type: WorkflowEntityType
    entity_id: UUID
    priority: str | None = Field(default=None, max_length=40)
    status: SlaClockStatus = SlaClockStatus.running
    started_at: datetime
    paused_at: datetime | None = None
    total_paused_seconds: int = Field(default=0, ge=0)
    due_at: datetime
    completed_at: datetime | None = None
    breached_at: datetime | None = None


class SlaClockCreate(BaseModel):
    policy_id: UUID
    entity_type: WorkflowEntityType
    entity_id: UUID
    priority: str | None = Field(default=None, max_length=40)
    started_at: datetime | None = None


class SlaClockUpdate(BaseModel):
    status: SlaClockStatus | None = None
    paused_at: datetime | None = None
    total_paused_seconds: int | None = Field(default=None, ge=0)
    due_at: datetime | None = None
    completed_at: datetime | None = None
    breached_at: datetime | None = None


class SlaClockRead(SlaClockBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class SlaBreachBase(BaseModel):
    clock_id: UUID
    status: SlaBreachStatus = SlaBreachStatus.open
    breached_at: datetime
    notes: str | None = None


class SlaBreachCreate(BaseModel):
    clock_id: UUID
    breached_at: datetime | None = None
    notes: str | None = None


class SlaBreachUpdate(BaseModel):
    status: SlaBreachStatus | None = None
    notes: str | None = None


class SlaBreachRead(SlaBreachBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class StatusTransitionRequest(BaseModel):
    to_status: str = Field(min_length=1, max_length=40)
    note: str | None = None
