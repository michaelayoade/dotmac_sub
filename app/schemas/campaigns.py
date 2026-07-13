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
    from_name: str | None = Field(default=None, max_length=160)
    from_email: str | None = Field(default=None, max_length=255)
    reply_to: str | None = Field(default=None, max_length=255)
    whatsapp_template_name: str | None = Field(default=None, max_length=200)
    whatsapp_template_language: str | None = Field(default="en", max_length=10)
    whatsapp_template_components: dict | None = None
    segment_filter: dict | None = None
    scheduled_at: datetime | None = None
    send_window_start_hour: int | None = Field(default=None, ge=0, le=23)
    send_window_end_hour: int | None = Field(default=None, ge=0, le=23)
    send_window_timezone: str | None = Field(default=None, max_length=64)
    campaign_sender_id: UUID | None = None
    campaign_smtp_config_id: UUID | None = None
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
    from_name: str | None = Field(default=None, max_length=160)
    from_email: str | None = Field(default=None, max_length=255)
    reply_to: str | None = Field(default=None, max_length=255)
    whatsapp_template_name: str | None = Field(default=None, max_length=200)
    whatsapp_template_language: str | None = Field(default=None, max_length=10)
    whatsapp_template_components: dict | None = None
    segment_filter: dict | None = None
    scheduled_at: datetime | None = None
    send_window_start_hour: int | None = Field(default=None, ge=0, le=23)
    send_window_end_hour: int | None = Field(default=None, ge=0, le=23)
    send_window_timezone: str | None = Field(default=None, max_length=64)
    campaign_sender_id: UUID | None = None
    campaign_smtp_config_id: UUID | None = None
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
    campaign_smtp_config_id: UUID | None = None
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
    from_name: str | None = Field(default=None, max_length=160)
    from_email: str | None = Field(default=None, max_length=255)
    reply_to: str | None = Field(default=None, max_length=255)
    campaign_smtp_config_id: UUID | None = None
    is_active: bool = True
    metadata: dict | None = None


class CampaignSenderUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str | None = Field(default=None, max_length=160)
    sender_key: str | None = Field(default=None, max_length=120)
    from_name: str | None = Field(default=None, max_length=160)
    from_email: str | None = Field(default=None, max_length=255)
    reply_to: str | None = Field(default=None, max_length=255)
    campaign_smtp_config_id: UUID | None = None
    is_active: bool | None = None
    metadata: dict | None = None


class CampaignSenderRead(BaseModel):
    id: UUID
    name: str
    sender_key: str
    from_name: str | None = None
    from_email: str | None = None
    reply_to: str | None = None
    campaign_smtp_config_id: UUID | None = None
    is_active: bool
    metadata: dict | None = None
    created_at: datetime
    updated_at: datetime


class CampaignSmtpConfigCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=160)
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=587, ge=1, le=65535)
    username: str | None = Field(default=None, max_length=255)
    password: str | None = Field(default=None, max_length=255)
    use_tls: bool = True
    use_ssl: bool = False
    is_active: bool = True
    metadata: dict | None = None


class CampaignSmtpConfigUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str | None = Field(default=None, max_length=160)
    host: str | None = Field(default=None, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str | None = Field(default=None, max_length=255)
    password: str | None = Field(default=None, max_length=255)
    use_tls: bool | None = None
    use_ssl: bool | None = None
    is_active: bool | None = None
    metadata: dict | None = None


class CampaignSmtpConfigRead(BaseModel):
    """SMTP profile as returned by the API — the password is never serialised."""

    id: UUID
    name: str
    host: str
    port: int
    username: str | None = None
    has_password: bool = False
    use_tls: bool
    use_ssl: bool
    is_active: bool
    metadata: dict | None = None
    created_at: datetime
    updated_at: datetime


class CampaignSuppressionCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    channel: str = "email"
    address: str = Field(min_length=1, max_length=255)
    reason: str = "manual"
    source: str | None = Field(default=None, max_length=80)
    subscriber_id: UUID | None = None
    campaign_id: UUID | None = None
    notes: str | None = None


class CampaignSuppressionRead(BaseModel):
    id: UUID
    channel: str
    address: str
    reason: str
    source: str | None = None
    subscriber_id: UUID | None = None
    campaign_id: UUID | None = None
    notes: str | None = None
    created_at: datetime


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
    sent: int
    failed: int
    skipped: int
    completed: bool
    suppressed: int = 0
