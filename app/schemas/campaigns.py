from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


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
    sending_started_at: datetime | None = None
    completed_at: datetime | None = None
    total_recipients: int
    sent_count: int
    delivered_count: int
    failed_count: int
    opened_count: int
    clicked_count: int
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
    failed_reason: str | None = None
    metadata: dict | None = None
    created_at: datetime


class CampaignAudienceBuildRead(BaseModel):
    campaign_id: UUID
    created: int
    skipped: int
    existing: int
    total_recipients: int
    skipped_reasons: dict[str, int] = Field(default_factory=dict)


class CampaignSendRead(BaseModel):
    campaign_id: UUID
    sent: int
    failed: int
    skipped: int
    completed: bool
