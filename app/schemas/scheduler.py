from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.scheduler import ScheduleType


class ScheduledTaskBase(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    task_name: str = Field(min_length=1, max_length=200)
    schedule_type: ScheduleType = ScheduleType.interval
    interval_seconds: int = 3600
    args_json: list | None = None
    kwargs_json: dict | None = None
    enabled: bool = True


class ScheduledTaskCreate(ScheduledTaskBase):
    pass


class ScheduledTaskUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    task_name: str | None = Field(default=None, min_length=1, max_length=200)
    schedule_type: ScheduleType | None = None
    interval_seconds: int | None = None
    args_json: list | None = None
    kwargs_json: dict | None = None
    enabled: bool | None = None


class ScheduledTaskRead(ScheduledTaskBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    last_run_at: datetime | None
    created_at: datetime
    updated_at: datetime
