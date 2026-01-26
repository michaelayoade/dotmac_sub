from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.snmp import SnmpAuthProtocol, SnmpPrivProtocol, SnmpVersion


class SnmpCredentialBase(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    version: SnmpVersion
    community_hash: str | None = Field(default=None, max_length=255)
    username: str | None = Field(default=None, max_length=120)
    auth_protocol: SnmpAuthProtocol = SnmpAuthProtocol.none
    auth_secret_hash: str | None = Field(default=None, max_length=255)
    priv_protocol: SnmpPrivProtocol = SnmpPrivProtocol.none
    priv_secret_hash: str | None = Field(default=None, max_length=255)
    is_active: bool = True


class SnmpCredentialCreate(SnmpCredentialBase):
    pass


class SnmpCredentialUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    version: SnmpVersion | None = None
    community_hash: str | None = Field(default=None, max_length=255)
    username: str | None = Field(default=None, max_length=120)
    auth_protocol: SnmpAuthProtocol | None = None
    auth_secret_hash: str | None = Field(default=None, max_length=255)
    priv_protocol: SnmpPrivProtocol | None = None
    priv_secret_hash: str | None = Field(default=None, max_length=255)
    is_active: bool | None = None


class SnmpCredentialRead(SnmpCredentialBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class SnmpTargetBase(BaseModel):
    device_id: UUID | None = None
    hostname: str | None = Field(default=None, max_length=160)
    mgmt_ip: str | None = Field(default=None, max_length=64)
    port: int = 161
    credential_id: UUID
    is_active: bool = True
    notes: str | None = None


class SnmpTargetCreate(SnmpTargetBase):
    pass


class SnmpTargetUpdate(BaseModel):
    device_id: UUID | None = None
    hostname: str | None = Field(default=None, max_length=160)
    mgmt_ip: str | None = Field(default=None, max_length=64)
    port: int | None = None
    credential_id: UUID | None = None
    is_active: bool | None = None
    notes: str | None = None


class SnmpTargetRead(SnmpTargetBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class SnmpOidBase(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    oid: str = Field(min_length=1, max_length=120)
    unit: str | None = Field(default=None, max_length=40)
    description: str | None = None
    is_active: bool = True


class SnmpOidCreate(SnmpOidBase):
    pass


class SnmpOidUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    oid: str | None = Field(default=None, min_length=1, max_length=120)
    unit: str | None = Field(default=None, max_length=40)
    description: str | None = None
    is_active: bool | None = None


class SnmpOidRead(SnmpOidBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class SnmpPollerBase(BaseModel):
    target_id: UUID
    oid_id: UUID
    poll_interval_sec: int = 60
    is_active: bool = True


class SnmpPollerCreate(SnmpPollerBase):
    pass


class SnmpPollerUpdate(BaseModel):
    target_id: UUID | None = None
    oid_id: UUID | None = None
    poll_interval_sec: int | None = None
    is_active: bool | None = None


class SnmpPollerRead(SnmpPollerBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class SnmpReadingBase(BaseModel):
    poller_id: UUID
    value: int = 0
    recorded_at: datetime


class SnmpReadingCreate(SnmpReadingBase):
    pass


class SnmpReadingUpdate(BaseModel):
    poller_id: UUID | None = None
    value: int | None = None
    recorded_at: datetime | None = None


class SnmpReadingRead(SnmpReadingBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
