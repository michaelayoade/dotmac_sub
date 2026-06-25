from __future__ import annotations

from datetime import date, datetime
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
    topup_gb: Decimal = Decimal("0")
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
    topup_gb: Decimal | None = None
    overage_gb: Decimal | None = None


class QuotaBucketRead(QuotaBucketBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime
    # Running cost of the current overage (overage_gb × the allowance's
    # overage_rate). Populated on customer-facing reads; None when not in
    # overage or no rate is configured.
    overage_amount: Decimal | None = None


class RadiusAccountingSessionBase(BaseModel):
    subscription_id: UUID
    access_credential_id: UUID
    radius_client_id: UUID | None = None
    nas_device_id: UUID | None = None
    session_id: str = Field(min_length=1, max_length=120)
    status_type: AccountingStatus
    session_start: datetime | None = None
    session_end: datetime | None = None
    # Most recent accounting observation (interim update or stop); a live
    # session keeps advancing this, an open one gone quiet is a ghost.
    last_update_at: datetime | None = None
    input_octets: int | None = None
    output_octets: int | None = None
    terminate_cause: str | None = Field(default=None, max_length=120)
    framed_ip_address: str | None = Field(default=None, max_length=64)
    framed_ipv6_prefix: str | None = Field(default=None, max_length=128)
    delegated_ipv6_prefix: str | None = Field(default=None, max_length=128)
    nas_port_id: str | None = Field(default=None, max_length=64)
    called_station_id: str | None = Field(default=None, max_length=64)


class RadiusAccountingSessionCreate(RadiusAccountingSessionBase):
    calling_station_id: str | None = Field(default=None, max_length=64)


class RadiusAccountingSessionUpdate(BaseModel):
    subscription_id: UUID | None = None
    access_credential_id: UUID | None = None
    radius_client_id: UUID | None = None
    nas_device_id: UUID | None = None
    session_id: str | None = Field(default=None, max_length=120)
    status_type: AccountingStatus | None = None
    session_start: datetime | None = None
    session_end: datetime | None = None
    last_update_at: datetime | None = None
    input_octets: int | None = None
    output_octets: int | None = None
    terminate_cause: str | None = Field(default=None, max_length=120)
    framed_ip_address: str | None = Field(default=None, max_length=64)
    framed_ipv6_prefix: str | None = Field(default=None, max_length=128)
    delegated_ipv6_prefix: str | None = Field(default=None, max_length=128)
    nas_port_id: str | None = Field(default=None, max_length=64)
    called_station_id: str | None = Field(default=None, max_length=64)
    calling_station_id: str | None = Field(default=None, max_length=64)


class RadiusAccountingSessionRead(RadiusAccountingSessionBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime


class UsageSeriesPoint(BaseModel):
    bucket_start: datetime
    bytes: int


class FupSummary(BaseModel):
    """Customer-facing Fair-Usage status for the caller's subscription.

    Derived from the per-subscription ``FupState`` the enforcement engine
    maintains; surfaced so the app can tell a subscriber they're throttled,
    how to restore speed, and when the limit resets.
    """

    status: str = Field(description="full_speed | approaching | throttled | blocked")
    is_reduced: bool = False
    speed_reduction_percent: float | None = None
    active_rule_name: str | None = None
    resets_at: datetime | None = None
    # Plain-language explainer for the active rule, e.g.
    # "Speed reduced to 25% after 100 GB this month".
    summary: str | None = None
    # Headroom against the nearest throttle/block rule — present even while
    # healthy so the app can pre-warn before enforcement.
    threshold_gb: float | None = None
    used_gb: float | None = None
    gb_until_throttle: float | None = None
    usage_ratio: float | None = None
    # Policy terms shown regardless of state, e.g.
    # "Speed reduces to 25% after 500 GB each month".
    policy_summary: str | None = None


class UsageSummaryResponse(BaseModel):
    """Time-windowed data-usage summary for GET /me/usage-summary."""

    period: str = Field(description="hour | today | week | cycle | all")
    start: datetime
    end: datetime
    total_bytes: int
    # Where the headline total came from: "samples" (integrated throughput),
    # "sessions" (RADIUS accounting octets), or "quota" (rated billing usage).
    total_source: str
    # True when the total is billing-grade (quota / session octets) rather than
    # reconstructed from the throughput series.
    is_authoritative: bool
    # Bucket width of the series: "minute" | "hour" | "day" | None (no chart).
    bucket: str | None = None
    # Mean throughput over the window (rx+tx bits/s) — the "average speed".
    # None for windows with no sample points (e.g. "all").
    average_bps: float | None = None
    series: list[UsageSeriesPoint] = Field(default_factory=list)
    # Fair-Usage status for the caller (None when no FUP applies / unknown).
    fup: FupSummary | None = None


class DailyUsagePoint(BaseModel):
    """One day's upload/download volume (bytes), summed across the caller's
    subscriptions."""

    date: date
    upload_bytes: int
    download_bytes: int
    total_bytes: int


class DailyUsageHistoryResponse(BaseModel):
    """Long-history daily usage for GET /me/usage-history.

    Sourced from the historical daily rollup (Splynx ``traffic_counter``
    backfill), which reaches years further back than per-session accounting.
    """

    start: date
    end: date
    total_upload_bytes: int
    total_download_bytes: int
    total_bytes: int
    points: list[DailyUsagePoint] = Field(default_factory=list)


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
    subscriber_id: UUID = Field(
        validation_alias="account_id", serialization_alias="account_id"
    )
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
