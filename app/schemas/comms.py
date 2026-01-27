from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.comms import CustomerNotificationStatus


class CustomerNotificationBase(BaseModel):
    entity_type: str = Field(min_length=1, max_length=40)
    entity_id: UUID
    channel: str = Field(min_length=1, max_length=40)
    recipient: str = Field(min_length=1, max_length=255)
    message: str = Field(min_length=1)
    status: CustomerNotificationStatus = CustomerNotificationStatus.pending
    sent_at: datetime | None = None


class CustomerNotificationCreate(CustomerNotificationBase):
    pass


class CustomerNotificationUpdate(BaseModel):
    status: CustomerNotificationStatus | None = None
    sent_at: datetime | None = None


class CustomerNotificationRead(CustomerNotificationBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime


class EtaUpdateBase(BaseModel):
    service_order_id: UUID
    eta_at: datetime
    note: str | None = None


class EtaUpdateCreate(EtaUpdateBase):
    pass


class EtaUpdateRead(EtaUpdateBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime


class SurveyBase(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str | None = None
    questions: list[dict] | None = None
    is_active: bool = True


class SurveyCreate(SurveyBase):
    pass


class SurveyUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = None
    questions: list[dict] | None = None
    is_active: bool | None = None


class SurveyRead(SurveyBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class SurveyResponseBase(BaseModel):
    survey_id: UUID
    responses: dict | None = None
    rating: int | None = Field(default=None, ge=1, le=5)


class SurveyResponseCreate(SurveyResponseBase):
    pass


class SurveyResponseRead(SurveyResponseBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
