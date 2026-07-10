"""Leads / pipeline / quotes schemas — CRM port (Phase 3 §1.3–§1.4).

Ported from ``dotmac_crm/app/schemas/crm/sales.py`` with the Phase 3 deltas:
the customer party is ``subscriber_id`` (sub ``subscribers``) instead of the
CRM ``person_id``, and staff / Phase 4 references (``owner_person_id``,
``owner_agent_id``, campaign columns) are plain UUIDs.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.sales import LeadStatus, QuoteStatus


class PipelineBase(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    is_active: bool = True
    metadata_: dict | None = Field(default=None, serialization_alias="metadata")


class PipelineCreate(PipelineBase):
    pass


class PipelineUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    is_active: bool | None = None
    metadata_: dict | None = Field(default=None, serialization_alias="metadata")


class PipelineRead(PipelineBase):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class PipelineStageBase(BaseModel):
    pipeline_id: UUID
    name: str = Field(min_length=1, max_length=160)
    order_index: int = 0
    is_active: bool = True
    default_probability: int = Field(default=50, ge=0, le=100)
    metadata_: dict | None = Field(default=None, serialization_alias="metadata")


class PipelineStageCreate(PipelineStageBase):
    pass


class PipelineStageUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    order_index: int | None = None
    is_active: bool | None = None
    default_probability: int | None = Field(default=None, ge=0, le=100)
    metadata_: dict | None = Field(default=None, serialization_alias="metadata")


class PipelineStageRead(PipelineStageBase):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class LeadBase(BaseModel):
    """Lead linked to a subscriber in the unified party model (§1.3)."""

    subscriber_id: UUID  # Required — links to Subscriber
    pipeline_id: UUID | None = None
    stage_id: UUID | None = None
    # CrmAgent UUID carried verbatim — Phase 4 inbox model (§1.8).
    owner_agent_id: UUID | None = None
    title: str | None = Field(default=None, max_length=200)
    status: LeadStatus = LeadStatus.new
    estimated_value: Decimal | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    probability: int | None = Field(default=None, ge=0, le=100)
    expected_close_date: date | None = None
    lost_reason: str | None = Field(default=None, max_length=200)
    lead_source: str | None = Field(default=None, max_length=40)
    campaign_id: UUID | None = None
    campaign_recipient_id: UUID | None = None
    region: str | None = Field(default=None, max_length=80)
    address: str | None = None
    notes: str | None = None
    metadata_: dict | None = Field(default=None, serialization_alias="metadata")
    is_active: bool = True


class LeadCreate(LeadBase):
    pass


class LeadUpdate(BaseModel):
    subscriber_id: UUID | None = None
    pipeline_id: UUID | None = None
    stage_id: UUID | None = None
    owner_agent_id: UUID | None = None
    title: str | None = Field(default=None, max_length=200)
    status: LeadStatus | None = None
    estimated_value: Decimal | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    probability: int | None = Field(default=None, ge=0, le=100)
    expected_close_date: date | None = None
    lost_reason: str | None = Field(default=None, max_length=200)
    lead_source: str | None = Field(default=None, max_length=40)
    region: str | None = Field(default=None, max_length=80)
    address: str | None = None
    notes: str | None = None
    metadata_: dict | None = Field(default=None, serialization_alias="metadata")
    is_active: bool | None = None


class LeadRead(LeadBase):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: UUID
    weighted_value: Decimal | None = None
    closed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class QuoteBase(BaseModel):
    """Quote linked to a subscriber in the unified party model (§1.4)."""

    subscriber_id: UUID  # Required — links to Subscriber
    lead_id: UUID | None = None
    # Staff person UUID carried verbatim — staff map for display (§1.8).
    owner_person_id: UUID | None = None
    status: QuoteStatus = QuoteStatus.draft
    currency: str = Field(default="NGN", min_length=3, max_length=3)
    subtotal: Decimal = Decimal("0.00")
    tax_rate: Decimal | None = Field(default=None, ge=0, le=100)
    tax_total: Decimal = Decimal("0.00")
    total: Decimal = Decimal("0.00")
    expires_at: datetime | None = None
    sent_at: datetime | None = None
    notes: str | None = None
    metadata_: dict | None = Field(default=None, serialization_alias="metadata")
    is_active: bool = True


class QuoteCreate(QuoteBase):
    pass


class QuoteUpdate(BaseModel):
    subscriber_id: UUID | None = None
    lead_id: UUID | None = None
    owner_person_id: UUID | None = None
    status: QuoteStatus | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    subtotal: Decimal | None = None
    tax_rate: Decimal | None = Field(default=None, ge=0, le=100)
    tax_total: Decimal | None = None
    total: Decimal | None = None
    expires_at: datetime | None = None
    sent_at: datetime | None = None
    notes: str | None = None
    metadata_: dict | None = Field(default=None, serialization_alias="metadata")
    is_active: bool | None = None


class QuoteRead(QuoteBase):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: UUID
    sales_order_id: UUID | None = None
    created_at: datetime
    updated_at: datetime


class QuoteLineItemBase(BaseModel):
    quote_id: UUID
    # CRM inventory UUID carried verbatim — inventory is Phase 5 (§1.4).
    inventory_item_id: UUID | None = None
    description: str = Field(min_length=1, max_length=255)
    quantity: Decimal = Field(default=Decimal("1.000"), gt=0)
    unit_price: Decimal = Field(default=Decimal("0.00"), ge=0)
    discount_percent: Decimal = Field(default=Decimal("0.00"), ge=0, le=100)
    amount: Decimal = Field(default=Decimal("0.00"), ge=0)
    metadata_: dict | None = Field(default=None, serialization_alias="metadata")


class QuoteLineItemCreate(QuoteLineItemBase):
    pass


class QuoteLineItemUpdate(BaseModel):
    inventory_item_id: UUID | None = None
    description: str | None = Field(default=None, min_length=1, max_length=255)
    quantity: Decimal | None = Field(default=None, gt=0)
    unit_price: Decimal | None = Field(default=None, ge=0)
    discount_percent: Decimal | None = Field(default=None, ge=0, le=100)
    amount: Decimal | None = Field(default=None, ge=0)
    metadata_: dict | None = Field(default=None, serialization_alias="metadata")


class QuoteLineItemRead(QuoteLineItemBase):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: UUID
    created_at: datetime
