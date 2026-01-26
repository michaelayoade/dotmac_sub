from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.tr069 import Tr069Event, Tr069JobStatus


class Tr069AcsServerBase(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    base_url: str = Field(min_length=1, max_length=255)
    is_active: bool = True
    notes: str | None = None


class Tr069AcsServerCreate(Tr069AcsServerBase):
    pass


class Tr069AcsServerUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    base_url: str | None = Field(default=None, min_length=1, max_length=255)
    is_active: bool | None = None
    notes: str | None = None


class Tr069AcsServerRead(Tr069AcsServerBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class Tr069CpeDeviceBase(BaseModel):
    acs_server_id: UUID
    cpe_device_id: UUID | None = None
    serial_number: str | None = Field(default=None, max_length=120)
    oui: str | None = Field(default=None, max_length=8)
    product_class: str | None = Field(default=None, max_length=120)
    connection_request_url: str | None = Field(default=None, max_length=255)
    last_inform_at: datetime | None = None
    is_active: bool = True


class Tr069CpeDeviceCreate(Tr069CpeDeviceBase):
    pass


class Tr069CpeDeviceUpdate(BaseModel):
    acs_server_id: UUID | None = None
    cpe_device_id: UUID | None = None
    serial_number: str | None = Field(default=None, max_length=120)
    oui: str | None = Field(default=None, max_length=8)
    product_class: str | None = Field(default=None, max_length=120)
    connection_request_url: str | None = Field(default=None, max_length=255)
    last_inform_at: datetime | None = None
    is_active: bool | None = None


class Tr069CpeDeviceRead(Tr069CpeDeviceBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class Tr069SessionBase(BaseModel):
    device_id: UUID
    event_type: Tr069Event
    request_id: str | None = Field(default=None, max_length=120)
    inform_payload: dict | None = None
    started_at: datetime
    ended_at: datetime | None = None
    notes: str | None = None


class Tr069SessionCreate(Tr069SessionBase):
    pass


class Tr069SessionUpdate(BaseModel):
    device_id: UUID | None = None
    event_type: Tr069Event | None = None
    request_id: str | None = Field(default=None, max_length=120)
    inform_payload: dict | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    notes: str | None = None


class Tr069SessionRead(Tr069SessionBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime


class Tr069ParameterBase(BaseModel):
    device_id: UUID
    name: str = Field(min_length=1, max_length=255)
    value: str | None = None
    updated_at: datetime


class Tr069ParameterCreate(Tr069ParameterBase):
    pass


class Tr069ParameterUpdate(BaseModel):
    device_id: UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=255)
    value: str | None = None
    updated_at: datetime | None = None


class Tr069ParameterRead(Tr069ParameterBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID


class Tr069JobBase(BaseModel):
    device_id: UUID
    name: str = Field(min_length=1, max_length=160)
    command: str = Field(min_length=1, max_length=160)
    payload: dict | None = None
    status: Tr069JobStatus = Tr069JobStatus.queued
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None


class Tr069JobCreate(Tr069JobBase):
    pass


class Tr069JobUpdate(BaseModel):
    device_id: UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=160)
    command: str | None = Field(default=None, min_length=1, max_length=160)
    payload: dict | None = None
    status: Tr069JobStatus | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None


class Tr069JobRead(Tr069JobBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime
