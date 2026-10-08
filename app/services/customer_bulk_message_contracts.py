"""Typed customer bulk-message requests, receipts, and delivery projections."""

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class CustomerMessageFilters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    search: str = ""
    status: str = ""
    customer_type: str = ""
    billing_mode: str = ""
    nas_id: str = ""
    pop_site_id: str = ""
    infrastructure_type: str = ""
    infrastructure_id: str = ""


class CustomerMessageSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    mode: Literal["selected", "filtered"]
    ids: tuple[UUID, ...] = ()
    filters: CustomerMessageFilters = Field(default_factory=CustomerMessageFilters)
    expected_count: int | None = Field(default=None, ge=0)
    expected_scope_token: str | None = None


class TemplateVariableSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source: str = ""
    custom_value: str = ""


class BulkMessageSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    channel: Literal["email", "sms", "push", "whatsapp"]
    template_id: UUID
    selection: CustomerMessageSelection | None = None
    customer_ids: tuple[UUID, ...] = ()
    template_variables: dict[str, str | TemplateVariableSource] = Field(
        default_factory=dict
    )
    expected_impact_token: str = ""
    confirmed: bool = False
    preview_only: bool = False


class BulkMessageCounts(BaseModel):
    model_config = ConfigDict(frozen=True)
    matched_count: int = 0
    created_count: int = 0
    queued_count: int = 0
    suppressed_count: int = 0
    skipped_count: int = 0
    notification_ids: tuple[UUID, ...] = ()


class BulkMessageSample(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: UUID
    name: str
    account_number: str | None = None
    recipient: str | None = None
    disposition: Literal["queued", "suppressed", "skipped"] | None = None
    reason_code: str | None = None
    reason: str | None = None


class BulkMessageEvaluation(BulkMessageCounts):
    success: bool = True
    preview: bool
    scope: Literal["selected", "filtered"]
    scope_token: str
    impact_token: str
    missing_ids: tuple[UUID, ...] = ()
    suppression_counts: dict[str, int] = Field(default_factory=dict)
    suppressed: tuple[BulkMessageSample, ...] = ()
    skipped: tuple[BulkMessageSample, ...] = ()
    recipient_summary: tuple[BulkMessageSample, ...] = ()
    recipient_summary_limit: int = 10
    render_sample_count: int = 0


class BulkSendState(StrEnum):
    accepted = "accepted"
    preparing = "preparing"
    queued = "queued"
    failed = "failed"


class BulkSendReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    request_id: UUID
    actor_id: UUID
    fingerprint: str
    spec: BulkMessageSpec
    state: BulkSendState = BulkSendState.accepted
    counts: BulkMessageCounts = Field(default_factory=BulkMessageCounts)
    attempts: int = 0
    accepted_at: datetime
    error: str | None = None


class BulkSendStatus(BaseModel):
    model_config = ConfigDict(frozen=True)
    request_id: UUID
    accepted: bool = True
    materialization_status: BulkSendState
    matched_count: int
    planned_queued_count: int
    planned_suppressed_count: int
    skipped_count: int
    delivered_count: int = 0
    submitted_count: int = 0
    pending_count: int = 0
    failed_count: int = 0
    canceled_count: int = 0
    error: str | None = None
    status_url: str
