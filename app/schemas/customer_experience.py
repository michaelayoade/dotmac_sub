from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class CustomerExperienceHandoffRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    subscriber_id: UUID
    subscription_id: UUID
    sales_order_id: UUID
    project_id: UUID
    installation_project_id: UUID
    service_order_id: UUID
    status: str
    policy_version: int
    readiness_evidence: dict
    ready_at: datetime | None = None
    accepted_at: datetime | None = None
    accepted_by_actor_type: str | None = None
    accepted_by_actor_id: str | None = None
    attention_reason: str | None = None
    created_at: datetime
    updated_at: datetime


class CustomerExperienceAcceptRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)


class CustomerExperienceAttentionRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)
