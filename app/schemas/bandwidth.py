from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class BandwidthSampleBase(BaseModel):
    subscription_id: UUID
    device_id: UUID | None = None
    interface_id: UUID | None = None
    rx_bps: int = 0
    tx_bps: int = 0
    sample_at: datetime


class BandwidthSampleCreate(BandwidthSampleBase):
    pass


class BandwidthSampleUpdate(BaseModel):
    subscription_id: UUID | None = None
    device_id: UUID | None = None
    interface_id: UUID | None = None
    rx_bps: int | None = None
    tx_bps: int | None = None
    sample_at: datetime | None = None


class BandwidthSampleRead(BandwidthSampleBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime


class BandwidthSeriesPoint(BaseModel):
    bucket_start: datetime
    rx_bps: float
    tx_bps: float
