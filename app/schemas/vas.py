"""Schemas for the VAS wallet (customer self-care)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class VasWalletEntryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    entry_type: str
    category: str
    amount: Decimal
    currency: str
    reference: str | None = None
    memo: str | None = None
    created_at: datetime

    @field_validator("entry_type", "category", mode="before")
    @classmethod
    def _enum_value(cls, value):
        return getattr(value, "value", value)


class VasWalletOverviewResponse(BaseModel):
    balance: Decimal
    currency: str
    auto_pay_bill_enabled: bool
    min_topup: int
    max_topup: int
    auth_threshold: int
    entries: list[VasWalletEntryRead] = []


class VasTopupInitiateRequest(BaseModel):
    amount: Decimal = Field(gt=0)


class VasTopupInitiateResponse(BaseModel):
    provider_type: str
    provider_public_key: str | None = None
    reference: str
    amount: Decimal
    currency: str = "NGN"
    customer_email: str | None = None


class VasTopupVerifyRequest(BaseModel):
    reference: str = Field(min_length=4, max_length=120)
    provider: str | None = None


class VasTopupVerifyResponse(BaseModel):
    amount: Decimal
    already_recorded: bool
    balance: Decimal


class VasPayBillRequest(BaseModel):
    amount: Decimal = Field(gt=0)


class VasPayBillResponse(BaseModel):
    payment_id: str
    amount: Decimal
    balance: Decimal


class VasAutoDeductUpdate(BaseModel):
    enabled: bool
