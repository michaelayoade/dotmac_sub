from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class KPIConfigBase(BaseModel):
    key: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=160)
    description: str | None = None
    parameters: dict | None = None
    is_active: bool = True


class KPIConfigCreate(KPIConfigBase):
    pass


class KPIConfigUpdate(BaseModel):
    key: str | None = Field(default=None, min_length=1, max_length=120)
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = None
    parameters: dict | None = None
    is_active: bool | None = None


class KPIConfigRead(KPIConfigBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class KPIAggregateBase(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    key: str = Field(min_length=1, max_length=120)
    period_start: datetime
    period_end: datetime
    value: Decimal
    metadata_: dict | None = Field(
        default=None,
        serialization_alias="metadata",
    )


class KPIAggregateCreate(KPIAggregateBase):
    pass


class KPIAggregateRead(KPIAggregateBase):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: UUID
    created_at: datetime


class KPIReadout(BaseModel):
    key: str
    value: Decimal
    label: str | None = None
