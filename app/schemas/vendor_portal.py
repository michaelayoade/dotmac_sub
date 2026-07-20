from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field


class VendorQuoteCreate(BaseModel):
    project_id: UUID
    currency: str = Field(default="NGN", min_length=3, max_length=3)
    vat_rate_percent: Decimal = Field(default=Decimal("0"), ge=0, le=100)


class VendorQuoteLineCreate(BaseModel):
    item_type: str | None = Field(default=None, max_length=80)
    description: str = Field(min_length=1, max_length=2000)
    cable_type: str | None = Field(default=None, max_length=120)
    fiber_count: int | None = Field(default=None, ge=1)
    splice_count: int | None = Field(default=None, ge=0)
    quantity: Decimal = Field(default=Decimal("1"), gt=0)
    unit_price: Decimal = Field(default=Decimal("0"), ge=0)
    notes: str | None = Field(default=None, max_length=2000)


class VendorQuoteLineUpdate(BaseModel):
    item_type: str | None = Field(default=None, max_length=80)
    description: str | None = Field(default=None, min_length=1, max_length=2000)
    cable_type: str | None = Field(default=None, max_length=120)
    fiber_count: int | None = Field(default=None, ge=1)
    splice_count: int | None = Field(default=None, ge=0)
    quantity: Decimal | None = Field(default=None, gt=0)
    unit_price: Decimal | None = Field(default=None, ge=0)
    notes: str | None = Field(default=None, max_length=2000)


class VendorRouteRevisionCreate(BaseModel):
    geojson: dict
    length_meters: float | None = Field(default=None, ge=0)


class VendorAsBuiltLineCreate(VendorQuoteLineCreate):
    pass


class VendorAsBuiltCreate(BaseModel):
    project_id: UUID
    proposed_revision_id: UUID | None = None
    geojson: dict | None = None
    actual_length_meters: float | None = Field(default=None, ge=0)
    variation_type: str | None = Field(default=None, max_length=40)
    variation_reason: str | None = Field(default=None, max_length=2000)
    work_order_ref: str | None = Field(default=None, max_length=120)
    line_items: list[VendorAsBuiltLineCreate] = Field(default_factory=list)


class VendorReview(BaseModel):
    review_notes: str | None = Field(default=None, max_length=2000)
