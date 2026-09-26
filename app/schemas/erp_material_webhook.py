from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ErpMaterialStatusLine(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: int = Field(ge=1)
    serial_numbers: tuple[str, ...] = ()
    item_code: str | None = Field(default=None, min_length=1, max_length=80)
    requested_qty: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    issued_qty: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    out_of_stock: bool = False

    @model_validator(mode="after")
    def validate_progress(self) -> Self:
        values = (self.item_code, self.requested_qty, self.issued_qty)
        if any(value is not None for value in values):
            if any(value is None for value in values):
                raise ValueError(
                    "Line progress requires item_code, requested_qty and issued_qty"
                )
            if (
                self.requested_qty is not None
                and self.issued_qty is not None
                and self.issued_qty > self.requested_qty
            ):
                raise ValueError("Issued quantity exceeds requested quantity")
        return self


class ErpMaterialStatusWebhook(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_request_id: UUID
    request_id: str = Field(min_length=1, max_length=120)
    request_number: str | None = Field(default=None, max_length=120)
    old_status: str | None = Field(default=None, max_length=40)
    new_status: str = Field(min_length=1, max_length=40)
    updated_at: datetime | None = None
    items: tuple[ErpMaterialStatusLine, ...] = ()
    fulfillment_version: Literal[1] | None = None

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        if self.fulfillment_version is not None:
            if not self.items or any(line.issued_qty is None for line in self.items):
                raise ValueError("Versioned fulfillment requires all line quantities")
            if self.updated_at is None or self.updated_at.tzinfo is None:
                raise ValueError(
                    "Versioned fulfillment requires an aware source timestamp"
                )
        return self


class ErpMaterialStatusReceipt(BaseModel):
    material_request_id: UUID
    status: str
    replayed: bool
