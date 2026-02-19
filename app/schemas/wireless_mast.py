from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models.wireless_mast import WirelessMastStatus


class WirelessMastCreate(BaseModel):
    name: str
    latitude: float
    longitude: float
    height_m: float | None = None
    structure_type: str | None = None
    owner: str | None = None
    status: WirelessMastStatus = WirelessMastStatus.active
    is_active: bool = True
    notes: str | None = None
    metadata_: dict | None = None
    pop_site_id: uuid.UUID | None = None


class WirelessMastUpdate(BaseModel):
    name: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    height_m: float | None = None
    structure_type: str | None = None
    owner: str | None = None
    status: WirelessMastStatus | None = None
    is_active: bool | None = None
    notes: str | None = None
    metadata_: dict | None = None
    pop_site_id: uuid.UUID | None = None


class WirelessMastRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    latitude: float
    longitude: float
    height_m: float | None = None
    structure_type: str | None = None
    owner: str | None = None
    status: WirelessMastStatus
    is_active: bool
    notes: str | None = None
    metadata_: dict | None = None
    pop_site_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime
