"""Typed contracts for Meta social inbox transport."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class MetaSocialChannel(StrEnum):
    """Supported Meta inbox channels with distinct authentication contracts."""

    facebook_messenger = "facebook_messenger"
    instagram_dm = "instagram_dm"


class MetaDirectMessageCommand(BaseModel):
    """One account-scoped reply requested by the Team Inbox delivery worker."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    channel: MetaSocialChannel
    provider_account_id: str = Field(min_length=1, max_length=120)
    recipient_id: str = Field(min_length=1, max_length=180)
    body: str = Field(min_length=1, max_length=10_000)
    correlation_id: str = Field(min_length=1, max_length=160)
    preview: bool = False


class MetaDirectMessageOutcome(BaseModel):
    """Sanitized provider outcome; no credential or raw response is retained."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    accepted: bool
    operation_status: str = Field(min_length=1, max_length=80)
    provider_message_id: str | None = Field(default=None, max_length=500)
    provider_recipient_id: str | None = Field(default=None, max_length=180)
    error_code: str | None = Field(default=None, max_length=120)


class MetaContactProfile(BaseModel):
    """Best-effort public profile fields used for Inbox display only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    display_name: str | None = Field(default=None, max_length=255)
    username: str | None = Field(default=None, max_length=255)
    profile_pic: str | None = Field(default=None, max_length=1000)


class MetaLeadField(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    values: tuple[str, ...] = Field(max_length=20)


class MetaLeadObservation(BaseModel):
    """Normalized Lead Ads observation fetched after a verified webhook."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    leadgen_id: str = Field(min_length=1, max_length=240)
    created_at: datetime
    page_id: str = Field(min_length=1, max_length=200)
    form_id: str = Field(min_length=1, max_length=200)
    campaign_id: str = Field(min_length=1, max_length=200)
    ad_set_id: str | None = Field(default=None, max_length=200)
    ad_id: str | None = Field(default=None, max_length=200)
    fields: tuple[MetaLeadField, ...] = Field(max_length=100)


class MetaLeadConversionCommand(BaseModel):
    """One customer-conversion projection for an original Meta Lead."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    leadgen_id: str = Field(min_length=1, max_length=240)
    converted_at: datetime
    event_id: str = Field(min_length=1, max_length=160)
    correlation_id: str = Field(min_length=1, max_length=160)
    preview: bool = False


class MetaLeadConversionOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    accepted: bool
    operation_status: str = Field(min_length=1, max_length=80)
    error_code: str | None = Field(default=None, max_length=120)


class MetaWebhookSecretMaterial(BaseModel):
    """Ephemeral inbound verification material resolved only at request time."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    signing_secret: str = Field(min_length=1, repr=False)
    verify_token: str = Field(min_length=1, repr=False)
