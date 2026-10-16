"""Typed command, query, and presentation boundary for customer occasions."""

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.services.owner_commands import CommandContext

READ_PERMISSION = "notification:occasion:read"
UPDATE_PERMISSION = "notification:occasion:update"
TEST_PERMISSION = "notification:occasion:test"


class CustomerOccasion(StrEnum):
    birthday = "birthday"
    anniversary = "anniversary"


class OccasionSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    occasion: CustomerOccasion
    enabled: bool = False
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=20000)
    timezone: str = Field(default="Africa/Lagos", min_length=1, max_length=80)
    send_hour: int = Field(default=9, ge=0, le=23)
    send_minute: int = Field(default=0, ge=0, le=59)
    revision: int = Field(default=1, ge=1)


@dataclass(frozen=True)
class SaveOccasionCommand:
    context: CommandContext
    settings: OccasionSettings


@dataclass(frozen=True)
class ProcessOccasionCommand:
    context: CommandContext
    occasion: CustomerOccasion
    now: datetime
    limit: int = 200


@dataclass(frozen=True)
class OccasionReviewQuery:
    now: datetime
    sample_limit: int = 10


@dataclass(frozen=True)
class OccasionRecipient:
    subscriber_id: UUID
    name: str
    email: str
    years: int
    reason: str | None


@dataclass(frozen=True)
class OccasionActivity:
    occasion: CustomerOccasion
    created_at: datetime
    status: str
    suppressed: tuple[str, ...]


@dataclass(frozen=True)
class OccasionReview:
    settings: OccasionSettings
    local_date: date
    matched_count: int
    missing_date_count: int
    recipients: tuple[OccasionRecipient, ...]
    subject_preview: str
    body_preview: str


@dataclass(frozen=True)
class OccasionReviewResult:
    evaluated_at: datetime
    automations: tuple[OccasionReview, ...]
    activity: tuple[OccasionActivity, ...]


@dataclass(frozen=True)
class OccasionProcessResult:
    occasion: CustomerOccasion
    processed: int = 0
    queued: int = 0
    suppressed: int = 0
    reason: str | None = None


@dataclass(frozen=True)
class OccasionDeliveryQuery:
    notification_id: UUID
    now: datetime


@dataclass(frozen=True)
class OccasionDeliveryDecision:
    allowed: bool
    reason: str | None = None


@dataclass(frozen=True)
class TestOccasionCommand:
    context: CommandContext
    occasion: CustomerOccasion
    recipient: EmailStr


@dataclass(frozen=True)
class OccasionTestResult:
    intent_id: UUID
    queued: bool
