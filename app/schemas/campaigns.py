from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.notification import NotificationChannel


class CampaignCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=200)
    campaign_type: str = "one_time"
    channel: str = "email"
    subject: str | None = Field(default=None, max_length=200)
    body_html: str | None = None
    body_text: str | None = None
    whatsapp_template_name: str | None = Field(default=None, max_length=200)
    whatsapp_template_language: str | None = Field(default="en", max_length=10)
    whatsapp_template_components: dict | None = None
    segment_filter: dict | None = None
    scheduled_at: datetime | None = None
    send_window_start_hour: int | None = Field(default=None, ge=0, le=23)
    send_window_end_hour: int | None = Field(default=None, ge=0, le=23)
    send_window_timezone: str | None = Field(default=None, max_length=64)
    campaign_sender_id: UUID | None = None
    service_team_id: UUID | None = None
    connector_config_id: UUID | None = None
    metadata: dict | None = None


class CampaignUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str | None = Field(default=None, max_length=200)
    status: str | None = Field(default=None, max_length=40)
    subject: str | None = Field(default=None, max_length=200)
    body_html: str | None = None
    body_text: str | None = None
    whatsapp_template_name: str | None = Field(default=None, max_length=200)
    whatsapp_template_language: str | None = Field(default=None, max_length=10)
    whatsapp_template_components: dict | None = None
    segment_filter: dict | None = None
    scheduled_at: datetime | None = None
    send_window_start_hour: int | None = Field(default=None, ge=0, le=23)
    send_window_end_hour: int | None = Field(default=None, ge=0, le=23)
    send_window_timezone: str | None = Field(default=None, max_length=64)
    campaign_sender_id: UUID | None = None
    service_team_id: UUID | None = None
    connector_config_id: UUID | None = None
    metadata: dict | None = None


class CampaignRead(BaseModel):
    id: UUID
    crm_campaign_id: UUID | None = None
    name: str
    campaign_type: str
    channel: str
    status: str
    subject: str | None = None
    scheduled_at: datetime | None = None
    send_window_start_hour: int | None = None
    send_window_end_hour: int | None = None
    send_window_timezone: str | None = None
    sending_started_at: datetime | None = None
    completed_at: datetime | None = None
    total_recipients: int
    sent_count: int
    delivered_count: int
    failed_count: int
    opened_count: int
    clicked_count: int
    campaign_sender_id: UUID | None = None
    service_team_id: UUID | None = None
    metadata: dict | None = None
    created_at: datetime
    updated_at: datetime


class CampaignRecipientRead(BaseModel):
    id: UUID
    campaign_id: UUID
    subscriber_id: UUID
    step_id: UUID | None = None
    address: str
    email: str | None = None
    status: str
    conversation_id: UUID | None = None
    message_id: UUID | None = None
    sent_at: datetime | None = None
    delivered_at: datetime | None = None
    suppressed_at: datetime | None = None
    attempt_count: int = 0
    last_attempt_at: datetime | None = None
    failed_reason: str | None = None
    metadata: dict | None = None
    created_at: datetime


class CampaignStepCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    step_index: int | None = Field(default=None, ge=0)
    name: str | None = Field(default=None, max_length=200)
    subject: str | None = Field(default=None, max_length=200)
    body_html: str | None = None
    body_text: str | None = None
    delay_days: int = Field(default=0, ge=0, le=365)
    delay_hours: int = Field(default=0, ge=0, le=23)
    is_active: bool = True


class CampaignStepUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    step_index: int | None = Field(default=None, ge=0)
    name: str | None = Field(default=None, max_length=200)
    subject: str | None = Field(default=None, max_length=200)
    body_html: str | None = None
    body_text: str | None = None
    delay_days: int | None = Field(default=None, ge=0, le=365)
    delay_hours: int | None = Field(default=None, ge=0, le=23)
    is_active: bool | None = None


class CampaignStepRead(BaseModel):
    id: UUID
    campaign_id: UUID
    step_index: int
    name: str | None = None
    subject: str | None = None
    body_html: str | None = None
    body_text: str | None = None
    delay_days: int
    delay_hours: int
    is_active: bool
    created_at: datetime
    updated_at: datetime


class CampaignSenderCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=160)
    sender_key: str = Field(min_length=1, max_length=120)
    is_active: bool = True
    metadata: dict | None = None


class CampaignSenderUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str | None = Field(default=None, max_length=160)
    sender_key: str | None = Field(default=None, max_length=120)
    is_active: bool | None = None
    metadata: dict | None = None


class CampaignSenderRead(BaseModel):
    id: UUID
    name: str
    sender_key: str
    is_active: bool
    metadata: dict | None = None
    created_at: datetime
    updated_at: datetime


class SuppressionCreate(BaseModel):
    """A platform suppression, created from the campaign admin surface.

    Scope is always ``marketing`` here: an operator suppressing someone from the
    campaign screen is recording a marketing refusal, not authorising us to stop
    sending their invoice. A hard bounce or an erasure request sets ``all``, and
    that is not something this endpoint does.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    channel: NotificationChannel = NotificationChannel.email
    address: str = Field(min_length=1, max_length=255)
    subscriber_id: UUID | None = None
    note: str | None = None


class CampaignUnsubscribeRead(BaseModel):
    unsubscribed: bool
    channel: str
    address: str


class CampaignAudienceBuildRead(BaseModel):
    campaign_id: UUID
    created: int
    skipped: int
    existing: int
    total_recipients: int
    skipped_reasons: dict[str, int] = Field(default_factory=dict)


class CampaignSendRead(BaseModel):
    campaign_id: UUID
    queued: int
    sent: int
    failed: int
    skipped: int
    completed: bool
    suppressed: int = 0
