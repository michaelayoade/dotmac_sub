from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.network_monitoring import AlertSeverity, AlertStatus
from app.models.notification import (
    DeliveryStatus,
    NotificationChannel,
    NotificationStatus,
)


class NotificationTemplateBase(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    code: str = Field(min_length=1, max_length=120)
    channel: NotificationChannel
    subject: str | None = Field(default=None, max_length=200)
    body: str = Field(min_length=1)
    is_active: bool = True


class NotificationTemplateCreate(NotificationTemplateBase):
    pass


class NotificationTemplateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    code: str | None = Field(default=None, min_length=1, max_length=120)
    channel: NotificationChannel | None = None
    subject: str | None = Field(default=None, max_length=200)
    body: str | None = Field(default=None, min_length=1)
    is_active: bool | None = None


class NotificationTemplateRead(NotificationTemplateBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class NotificationBase(BaseModel):
    template_id: UUID | None = None
    channel: NotificationChannel
    recipient: str = Field(min_length=1, max_length=255)
    subject: str | None = Field(default=None, max_length=200)
    body: str | None = None
    status: NotificationStatus = NotificationStatus.queued
    send_at: datetime | None = None
    sent_at: datetime | None = None
    last_error: str | None = None
    retry_count: int = Field(default=0, ge=0)
    is_active: bool = True


class NotificationCreate(NotificationBase):
    pass


class NotificationUpdate(BaseModel):
    template_id: UUID | None = None
    channel: NotificationChannel | None = None
    recipient: str | None = Field(default=None, max_length=255)
    subject: str | None = Field(default=None, max_length=200)
    body: str | None = None
    status: NotificationStatus | None = None
    send_at: datetime | None = None
    sent_at: datetime | None = None
    last_error: str | None = None
    retry_count: int | None = Field(default=None, ge=0)
    is_active: bool | None = None


class NotificationRead(NotificationBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class NotificationBulkCreateRequest(BaseModel):
    template_id: UUID | None = None
    channel: NotificationChannel
    recipients: list[str]
    subject: str | None = Field(default=None, max_length=200)
    body: str | None = None
    status: NotificationStatus = NotificationStatus.queued
    send_at: datetime | None = None


class NotificationBulkCreateResponse(BaseModel):
    created: int
    notification_ids: list[UUID]


class NotificationDeliveryBulkUpdateRequest(BaseModel):
    delivery_ids: list[UUID]
    status: DeliveryStatus
    response_code: str | None = Field(default=None, max_length=60)
    response_body: str | None = None
    provider_message_id: str | None = Field(default=None, max_length=200)
    occurred_at: datetime | None = None


class NotificationDeliveryBulkUpdateResponse(BaseModel):
    updated: int


class NotificationDeliveryBase(BaseModel):
    notification_id: UUID
    provider: str | None = Field(default=None, max_length=120)
    provider_message_id: str | None = Field(default=None, max_length=200)
    status: DeliveryStatus
    response_code: str | None = Field(default=None, max_length=60)
    response_body: str | None = None
    occurred_at: datetime | None = None
    is_active: bool = True


class NotificationDeliveryCreate(NotificationDeliveryBase):
    pass


class NotificationDeliveryUpdate(BaseModel):
    notification_id: UUID | None = None
    provider: str | None = Field(default=None, max_length=120)
    provider_message_id: str | None = Field(default=None, max_length=200)
    status: DeliveryStatus | None = None
    response_code: str | None = Field(default=None, max_length=60)
    response_body: str | None = None
    occurred_at: datetime | None = None
    is_active: bool | None = None


class NotificationDeliveryRead(NotificationDeliveryBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID


class AlertNotificationPolicyBase(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    channel: NotificationChannel
    recipient: str = Field(min_length=1, max_length=255)
    template_id: UUID | None = None
    connector_config_id: UUID | None = None
    rule_id: UUID | None = None
    device_id: UUID | None = None
    interface_id: UUID | None = None
    severity_min: AlertSeverity = AlertSeverity.warning
    status: AlertStatus = AlertStatus.open
    is_active: bool = True
    notes: str | None = None


class AlertNotificationPolicyCreate(AlertNotificationPolicyBase):
    pass


class AlertNotificationPolicyUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    channel: NotificationChannel | None = None
    recipient: str | None = Field(default=None, max_length=255)
    template_id: UUID | None = None
    connector_config_id: UUID | None = None
    rule_id: UUID | None = None
    device_id: UUID | None = None
    interface_id: UUID | None = None
    severity_min: AlertSeverity | None = None
    status: AlertStatus | None = None
    is_active: bool | None = None
    notes: str | None = None


class AlertNotificationPolicyRead(AlertNotificationPolicyBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class AlertNotificationLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    alert_id: UUID
    policy_id: UUID
    notification_id: UUID | None
    created_at: datetime


class OnCallRotationBase(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    timezone: str = Field(default="UTC", max_length=60)
    is_active: bool = True
    notes: str | None = None


class OnCallRotationCreate(OnCallRotationBase):
    pass


class OnCallRotationUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    timezone: str | None = Field(default=None, max_length=60)
    is_active: bool | None = None
    notes: str | None = None


class OnCallRotationRead(OnCallRotationBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class OnCallRotationMemberBase(BaseModel):
    rotation_id: UUID
    name: str = Field(min_length=1, max_length=120)
    contact: str = Field(min_length=1, max_length=255)
    priority: int = 0
    last_used_at: datetime | None = None
    is_active: bool = True


class OnCallRotationMemberCreate(OnCallRotationMemberBase):
    pass


class OnCallRotationMemberUpdate(BaseModel):
    rotation_id: UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=120)
    contact: str | None = Field(default=None, min_length=1, max_length=255)
    priority: int | None = None
    last_used_at: datetime | None = None
    is_active: bool | None = None


class OnCallRotationMemberRead(OnCallRotationMemberBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class AlertNotificationPolicyStepBase(BaseModel):
    policy_id: UUID
    step_index: int = 0
    delay_minutes: int = Field(default=0, ge=0)
    channel: NotificationChannel
    recipient: str | None = Field(default=None, max_length=255)
    template_id: UUID | None = None
    connector_config_id: UUID | None = None
    rotation_id: UUID | None = None
    severity_min: AlertSeverity = AlertSeverity.warning
    status: AlertStatus = AlertStatus.open
    is_active: bool = True


class AlertNotificationPolicyStepCreate(AlertNotificationPolicyStepBase):
    pass


class AlertNotificationPolicyStepUpdate(BaseModel):
    policy_id: UUID | None = None
    step_index: int | None = None
    delay_minutes: int | None = Field(default=None, ge=0)
    channel: NotificationChannel | None = None
    recipient: str | None = Field(default=None, max_length=255)
    template_id: UUID | None = None
    connector_config_id: UUID | None = None
    rotation_id: UUID | None = None
    severity_min: AlertSeverity | None = None
    status: AlertStatus | None = None
    is_active: bool | None = None


class AlertNotificationPolicyStepRead(AlertNotificationPolicyStepBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime
