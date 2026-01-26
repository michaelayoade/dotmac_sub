from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.integration import (
    IntegrationJobType,
    IntegrationRunStatus,
    IntegrationScheduleType,
    IntegrationTargetType,
)


class IntegrationTargetBase(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    target_type: IntegrationTargetType = IntegrationTargetType.custom
    connector_config_id: UUID | None = None
    is_active: bool = True
    notes: str | None = None


class IntegrationTargetCreate(IntegrationTargetBase):
    pass


class IntegrationTargetUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    target_type: IntegrationTargetType | None = None
    connector_config_id: UUID | None = None
    is_active: bool | None = None
    notes: str | None = None


class IntegrationTargetRead(IntegrationTargetBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class IntegrationJobBase(BaseModel):
    target_id: UUID
    name: str = Field(min_length=1, max_length=160)
    job_type: IntegrationJobType = IntegrationJobType.sync
    schedule_type: IntegrationScheduleType = IntegrationScheduleType.manual
    interval_minutes: int | None = None
    interval_seconds: int | None = None
    is_active: bool = True
    last_run_at: datetime | None = None
    notes: str | None = None


class IntegrationJobCreate(IntegrationJobBase):
    pass


class IntegrationJobUpdate(BaseModel):
    target_id: UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=160)
    job_type: IntegrationJobType | None = None
    schedule_type: IntegrationScheduleType | None = None
    interval_minutes: int | None = None
    interval_seconds: int | None = None
    is_active: bool | None = None
    last_run_at: datetime | None = None
    notes: str | None = None


class IntegrationJobRead(IntegrationJobBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class IntegrationRunBase(BaseModel):
    job_id: UUID
    status: IntegrationRunStatus = IntegrationRunStatus.running
    started_at: datetime
    finished_at: datetime | None = None
    error: str | None = None
    metrics: dict | None = None


class IntegrationRunRead(IntegrationRunBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
