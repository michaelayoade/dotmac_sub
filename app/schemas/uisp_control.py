from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.uisp_control import (
    UispIntentStatus,
    UispIntentTargetType,
    UispSnapshotSource,
)


class UispIntentStage(BaseModel):
    target_type: UispIntentTargetType
    target_id: UUID
    subscription_id: UUID | None = None
    service_order_id: UUID | None = None
    desired_config: dict = Field(default_factory=dict)


class UispIntentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    target_type: UispIntentTargetType
    target_id: UUID
    subscription_id: UUID | None
    service_order_id: UUID | None
    uisp_device_id: str | None
    desired_config: dict
    observed_config: dict | None
    drift: dict | None
    desired_revision: int
    verified_revision: int | None
    status: UispIntentStatus
    last_error: str | None
    last_observed_at: datetime | None
    last_verified_at: datetime | None
    created_at: datetime
    updated_at: datetime


class UispSnapshotRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    intent_id: UUID
    source: UispSnapshotSource
    revision: int | None
    config: dict
    redacted: bool
    created_at: datetime


class UispApplyRead(BaseModel):
    operation_id: UUID
    status: str
    applied: bool
    message: str
