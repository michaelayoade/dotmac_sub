from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from app.models.webhook import WebhookDeliveryStatus, WebhookEventType


class WebhookEndpointBase(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    url: str = Field(min_length=1, max_length=500)
    connector_config_id: UUID | None = None
    secret: str | None = Field(default=None, max_length=255)
    is_active: bool = True


class WebhookEndpointCreate(WebhookEndpointBase):
    pass


class WebhookEndpointUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=160)
    url: str | None = Field(default=None, max_length=500)
    connector_config_id: UUID | None = None
    secret: str | None = Field(default=None, max_length=255)
    is_active: bool | None = None


class WebhookEndpointRead(WebhookEndpointBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime

    @field_serializer("secret")
    def _mask_secret(self, value: str | None) -> str | None:
        if not value:
            return None
        suffix = value[-4:]
        return f"{'*' * max(len(value) - 4, 4)}{suffix}"


class WebhookSubscriptionBase(BaseModel):
    endpoint_id: UUID
    event_type: WebhookEventType = WebhookEventType.custom
    is_active: bool = True


class WebhookSubscriptionCreate(WebhookSubscriptionBase):
    pass


class WebhookSubscriptionUpdate(BaseModel):
    endpoint_id: UUID | None = None
    event_type: WebhookEventType | None = None
    is_active: bool | None = None


class WebhookSubscriptionRead(WebhookSubscriptionBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class WebhookDeliveryBase(BaseModel):
    subscription_id: UUID
    endpoint_id: UUID
    event_type: WebhookEventType = WebhookEventType.custom
    status: WebhookDeliveryStatus = WebhookDeliveryStatus.pending
    attempt_count: int = 0
    last_attempt_at: datetime | None = None
    delivered_at: datetime | None = None
    response_status: int | None = None
    error: str | None = None
    payload: dict | None = None


class WebhookDeliveryCreate(BaseModel):
    subscription_id: UUID
    event_type: WebhookEventType = WebhookEventType.custom
    payload: dict | None = None


class WebhookDeliveryUpdate(BaseModel):
    status: WebhookDeliveryStatus | None = None
    attempt_count: int | None = None
    last_attempt_at: datetime | None = None
    delivered_at: datetime | None = None
    response_status: int | None = None
    error: str | None = None


class WebhookDeliveryRead(WebhookDeliveryBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
