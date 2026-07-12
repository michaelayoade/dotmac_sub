from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class VendorPurchaseInvoiceLineCreate(BaseModel):
    item_type: str | None = Field(default=None, max_length=80)
    description: str = Field(min_length=1, max_length=2000)
    quantity: Decimal = Field(default=Decimal("1.000"), gt=0)
    unit_price: Decimal = Field(default=Decimal("0.00"), ge=0)
    notes: str | None = Field(default=None, max_length=2000)


class VendorPurchaseInvoiceLineUpdate(BaseModel):
    item_type: str | None = Field(default=None, max_length=80)
    description: str | None = Field(default=None, min_length=1, max_length=2000)
    quantity: Decimal | None = Field(default=None, gt=0)
    unit_price: Decimal | None = Field(default=None, ge=0)
    notes: str | None = Field(default=None, max_length=2000)


class VendorPurchaseInvoiceLineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    invoice_id: UUID
    item_type: str | None = None
    description: str | None = None
    quantity: Decimal
    unit_price: Decimal
    amount: Decimal
    notes: str | None = None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class VendorPurchaseInvoiceCreate(BaseModel):
    project_id: UUID
    invoice_number: str | None = Field(default=None, max_length=80)
    currency: str = Field(default="NGN", min_length=3, max_length=3)
    tax_rate_percent: Decimal = Field(default=Decimal("0.00"), ge=0, le=100)


class VendorPurchaseInvoiceUpdate(BaseModel):
    invoice_number: str | None = Field(default=None, max_length=80)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    tax_rate_percent: Decimal | None = Field(default=None, ge=0, le=100)


class VendorPurchaseInvoiceReview(BaseModel):
    review_notes: str | None = Field(default=None, max_length=2000)


class VendorPurchaseInvoiceRead(BaseModel):
    id: UUID
    project_id: UUID
    vendor_id: UUID
    invoice_number: str | None = None
    status: str
    currency: str
    tax_rate_percent: Decimal | None = None
    subtotal: Decimal
    tax_total: Decimal
    total: Decimal
    submitted_at: datetime | None = None
    reviewed_at: datetime | None = None
    reviewed_by_system_user_id: UUID | None = None
    review_notes: str | None = None
    created_by_system_user_id: UUID | None = None
    attachment_stored_file_id: UUID | None = None
    attachment_file_name: str | None = None
    attachment_content_type: str | None = None
    attachment_file_size: int | None = None
    erp_purchase_order_id: str | None = None
    erp_purchase_invoice_id: str | None = None
    erp_purchase_invoice_status: str | None = None
    erp_sync_error: str | None = None
    erp_synced_at: datetime | None = None
    erp_attachment_synced_at: datetime | None = None
    is_active: bool
    created_at: datetime
    updated_at: datetime
    line_items: list[VendorPurchaseInvoiceLineRead] = Field(default_factory=list)
