from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.radius import RadiusSyncStatus


class RadiusServerBase(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    host: str = Field(min_length=1, max_length=255)
    auth_port: int = 1812
    acct_port: int = 1813
    description: str | None = None
    is_active: bool = True


class RadiusServerCreate(RadiusServerBase):
    pass


class RadiusServerUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    host: str | None = Field(default=None, min_length=1, max_length=255)
    auth_port: int | None = None
    acct_port: int | None = None
    description: str | None = None
    is_active: bool | None = None


class RadiusServerRead(RadiusServerBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class RadiusClientBase(BaseModel):
    server_id: UUID
    nas_device_id: UUID | None = None
    client_ip: str = Field(min_length=1, max_length=64)
    shared_secret_hash: str = Field(min_length=1, max_length=255)
    description: str | None = None
    is_active: bool = True


class RadiusClientCreate(RadiusClientBase):
    pass


class RadiusClientUpdate(BaseModel):
    server_id: UUID | None = None
    nas_device_id: UUID | None = None
    client_ip: str | None = Field(default=None, min_length=1, max_length=64)
    shared_secret_hash: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    is_active: bool | None = None


class RadiusClientRead(RadiusClientBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class RadiusUserBase(BaseModel):
    account_id: UUID
    subscription_id: UUID | None = None
    access_credential_id: UUID
    username: str = Field(min_length=1, max_length=120)
    secret_hash: str | None = Field(default=None, max_length=255)
    radius_profile_id: UUID | None = None
    is_active: bool = True


class RadiusUserRead(RadiusUserBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    last_sync_at: datetime | None
    created_at: datetime
    updated_at: datetime


class RadiusSyncJobBase(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    server_id: UUID
    connector_config_id: UUID | None = None
    sync_users: bool = True
    sync_nas_clients: bool = True
    is_active: bool = True


class RadiusSyncJobCreate(RadiusSyncJobBase):
    pass


class RadiusSyncJobUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    server_id: UUID | None = None
    connector_config_id: UUID | None = None
    sync_users: bool | None = None
    sync_nas_clients: bool | None = None
    is_active: bool | None = None


class RadiusSyncJobRead(RadiusSyncJobBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    last_run_at: datetime | None
    created_at: datetime
    updated_at: datetime


class RadiusSyncRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    job_id: UUID
    status: RadiusSyncStatus
    started_at: datetime
    finished_at: datetime | None
    users_created: int
    users_updated: int
    clients_created: int
    clients_updated: int
    details: dict | None = None
