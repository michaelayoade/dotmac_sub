"""Schemas for the native referral program.

Ported from CRM ``app/schemas/crm/referral.py`` with the person→subscriber
re-keying: ``person_id`` → ``subscriber_id``, ``referrer_person_id`` →
``referrer_subscriber_id``, and ``referred_person_id`` collapsed into
``referred_subscriber_id``. New capture identifies ``referred_party_id`` first;
the Subscriber account remains optional until reviewed conversion. Statuses are
plain strings (String + app enum per sub convention, ).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.schemas.subscriber import SubscriberCreate


class ReferralCodeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    subscriber_id: UUID
    code: str
    is_active: bool
    created_at: datetime


class ReferralCaptureRequest(BaseModel):
    """Public capture payload: a prospect signing up via a referral code.

    Same validation contract as the CRM's public ``POST /referrals/capture``.
    """

    code: str = Field(min_length=1, max_length=24)
    name: str | None = Field(default=None, max_length=160)
    email: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=40)
    region: str | None = Field(default=None, max_length=80)
    address: str | None = Field(default=None, max_length=500)
    notes: str | None = Field(default=None, max_length=1000)


class ReferralRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    referrer_subscriber_id: UUID
    referral_code_id: UUID | None
    referred_party_id: UUID | None
    referred_subscriber_id: UUID | None
    referred_lead_id: UUID | None
    status: str
    reward_amount: Decimal | None
    reward_currency: str
    reward_status: str
    reward_issued_at: datetime | None
    qualified_at: datetime | None
    source: str | None
    notes: str | None
    created_at: datetime
    updated_at: datetime


class ReferralCaptureRead(ReferralRead):
    """Public capture result plus its short-lived signup capability."""

    conversion_token: str
    conversion_expires_at: datetime


class ReferralRejectRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=200)


class ReferralAccountContext(BaseModel):
    """Stable PII-free context carried into reviewed account conversion."""

    referred_party_id: UUID
    referred_lead_id: UUID


class ReferralSubscriberAttachRequest(ReferralAccountContext):
    subscriber_id: UUID
    reason: str = Field(min_length=1, max_length=1000)


class ReferralSubscriberCreateRequest(ReferralAccountContext):
    subscriber: SubscriberCreate
    reason: str = Field(min_length=1, max_length=1000)


class ReferralAccountConversionRead(BaseModel):
    referral_id: UUID
    referred_party_id: UUID
    referred_lead_id: UUID
    subscriber_id: UUID
    outcome: Literal["created", "attached", "already_attached"]


class ReferralSelfServiceSignupRead(ReferralAccountConversionRead):
    """Account result plus out-of-band credential enrollment delivery state."""

    enrollment_status: Literal[
        "queued",
        "rate_limited",
        "suppressed",
        "already_enrolled",
        "manual_review_required",
    ]
    enrollment_retry_after_seconds: int | None = None


class ReferralSelfServiceAccountCreate(BaseModel):
    """Narrow public account payload with no lifecycle or billing controls."""

    model_config = ConfigDict(extra="forbid")

    first_name: str = Field(min_length=1, max_length=80)
    last_name: str = Field(min_length=1, max_length=80)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=40)
    address_line1: str | None = Field(default=None, max_length=120)
    address_line2: str | None = Field(default=None, max_length=120)
    city: str | None = Field(default=None, max_length=80)
    region: str | None = Field(default=None, max_length=80)
    lga: str | None = Field(default=None, max_length=80)
    postal_code: str | None = Field(default=None, max_length=20)
    country_code: str | None = Field(default=None, max_length=2)
    locale: str | None = Field(default=None, max_length=16)
    timezone: str | None = Field(default=None, max_length=64)


class ReferralSelfServiceSignupRequest(BaseModel):
    conversion_token: str = Field(min_length=1, max_length=4096)
    account: ReferralSelfServiceAccountCreate
