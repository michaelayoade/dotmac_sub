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
    customer_display_name: str | None = None
    customer_email: str | None = None
    customer_phone: str | None = None
    sales_order_number: str | None = None
    sales_order_status: str | None = None
    sales_order_payment_status: str | None = None
    project_name: str | None = None
    project_type: str | None = None
    project_status: str | None = None
    service_order_status: str | None = None
    subscription_status: str | None = None
    offer_name: str | None = None
    ready_age_seconds: int | None = None
    next_action: str | None = None


class CustomerExperienceAcceptRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)


class CustomerExperienceAttentionRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class CustomerExperienceResolveAttentionRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)
