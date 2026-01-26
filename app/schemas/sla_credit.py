from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.sla_credit import SlaCreditReportStatus


class SlaCreditReportBase(BaseModel):
    period_start: datetime
    period_end: datetime
    status: SlaCreditReportStatus = SlaCreditReportStatus.draft
    notes: str | None = None


class SlaCreditReportCreate(SlaCreditReportBase):
    account_id: UUID | None = None


class SlaCreditReportUpdate(BaseModel):
    status: SlaCreditReportStatus | None = None
    notes: str | None = None


class SlaCreditItemBase(BaseModel):
    report_id: UUID
    account_id: UUID
    subscription_id: UUID | None = None
    invoice_id: UUID | None = None
    sla_profile_id: UUID | None = None
    target_percent: Decimal
    actual_percent: Decimal
    credit_percent: Decimal
    credit_amount: Decimal
    currency: str
    approved: bool
    memo: str | None = None


class SlaCreditItemUpdate(BaseModel):
    invoice_id: UUID | None = None
    target_percent: Decimal | None = Field(default=None, ge=0)
    actual_percent: Decimal | None = Field(default=None, ge=0)
    credit_percent: Decimal | None = Field(default=None, ge=0)
    credit_amount: Decimal | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    approved: bool | None = None
    memo: str | None = None


class SlaCreditItemRead(SlaCreditItemBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime


class SlaCreditReportRead(SlaCreditReportBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime
    items: list[SlaCreditItemRead] = Field(default_factory=list)


class SlaCreditApplyRequest(BaseModel):
    apply_all: bool = True
    item_ids: list[UUID] | None = None
    apply_to_invoices: bool = True


class SlaCreditApplyResult(BaseModel):
    report_id: UUID
    credit_notes_created: int
    items_applied: int
