from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.usage import (
    AccountingStatus,
    UsageChargeStatus,
    UsageRatingRunStatus,
    UsageSource,
)


class QuotaBucketBase(BaseModel):
    subscription_id: UUID
    period_start: datetime
    period_end: datetime
    included_gb: Decimal | None = None
    used_gb: Decimal = Decimal("0")
    rollover_gb: Decimal = Decimal("0")
    overage_gb: Decimal = Decimal("0")


class QuotaBucketCreate(QuotaBucketBase):
    pass


class QuotaBucketUpdate(BaseModel):
    subscription_id: UUID | None = None
    period_start: datetime | None = None
    period_end: datetime | None = None
    included_gb: Decimal | None = None
    used_gb: Decimal | None = None
    rollover_gb: Decimal | None = None
    overage_gb: Decimal | None = None


class QuotaBucketRead(QuotaBucketBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class RadiusAccountingSessionBase(BaseModel):
    subscription_id: UUID
    access_credential_id: UUID
    radius_client_id: UUID | None = None
    nas_device_id: UUID | None = None
    session_id: str = Field(min_length=1, max_length=120)
    status_type: AccountingStatus
    session_start: datetime | None = None
    session_end: datetime | None = None
    input_octets: int | None = None
    output_octets: int | None = None
    terminate_cause: str | None = Field(default=None, max_length=120)


class RadiusAccountingSessionCreate(RadiusAccountingSessionBase):
    pass


class RadiusAccountingSessionUpdate(BaseModel):
    subscription_id: UUID | None = None
    access_credential_id: UUID | None = None
    radius_client_id: UUID | None = None
    nas_device_id: UUID | None = None
    session_id: str | None = Field(default=None, max_length=120)
    status_type: AccountingStatus | None = None
    session_start: datetime | None = None
    session_end: datetime | None = None
    input_octets: int | None = None
    output_octets: int | None = None
    terminate_cause: str | None = Field(default=None, max_length=120)


class RadiusAccountingSessionRead(RadiusAccountingSessionBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime


class UsageRecordBase(BaseModel):
    subscription_id: UUID
    quota_bucket_id: UUID | None = None
    source: UsageSource
    recorded_at: datetime
    input_gb: Decimal = Decimal("0")
    output_gb: Decimal = Decimal("0")
    total_gb: Decimal = Decimal("0")


class UsageRecordCreate(UsageRecordBase):
    pass


class UsageRecordUpdate(BaseModel):
    subscription_id: UUID | None = None
    quota_bucket_id: UUID | None = None
    source: UsageSource | None = None
    recorded_at: datetime | None = None
    input_gb: Decimal | None = None
    output_gb: Decimal | None = None
    total_gb: Decimal | None = None


class UsageRecordRead(UsageRecordBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime


class UsageChargeBase(BaseModel):
    subscription_id: UUID
    account_id: UUID
    invoice_line_id: UUID | None = None
    period_start: datetime
    period_end: datetime
    total_gb: Decimal = Field(default=Decimal("0.0000"), ge=0)
    included_gb: Decimal = Field(default=Decimal("0.0000"), ge=0)
    billable_gb: Decimal = Field(default=Decimal("0.0000"), ge=0)
    unit_price: Decimal = Field(default=Decimal("0.0000"), ge=0)
    amount: Decimal = Field(default=Decimal("0.00"), ge=0)
    currency: str = Field(default="NGN", min_length=3, max_length=3)
    status: UsageChargeStatus = UsageChargeStatus.staged
    notes: str | None = None
    rated_at: datetime | None = None


class UsageChargeRead(UsageChargeBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class UsageRatingRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    run_at: datetime
    period_start: datetime
    period_end: datetime
    status: UsageRatingRunStatus
    subscriptions_scanned: int
    charges_created: int
    skipped: int
    error: str | None
    created_at: datetime


class UsageRatingRunRequest(BaseModel):
    period_start: datetime | None = None
    period_end: datetime | None = None
    subscription_id: UUID | None = None
    dry_run: bool = False


class UsageRatingRunResponse(BaseModel):
    run_id: UUID | None = None
    run_at: datetime
    period_start: datetime
    period_end: datetime
    subscriptions_scanned: int
    charges_created: int
    skipped: int


class UsageChargePostRequest(BaseModel):
    invoice_id: UUID | None = None


class UsageChargePostBatchRequest(BaseModel):
    period_start: datetime
    period_end: datetime
    account_id: UUID | None = None


class UsageChargePostBatchResponse(BaseModel):
    posted: int
