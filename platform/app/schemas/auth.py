from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from app.models.auth import AuthProvider, MFAMethodType, SessionStatus


class UserCredentialBase(BaseModel):
    person_id: UUID
    provider: AuthProvider = AuthProvider.local
    username: str | None = Field(default=None, max_length=150)
    password_hash: str | None = Field(default=None, max_length=255)
    must_change_password: bool = False
    password_updated_at: datetime | None = None
    failed_login_attempts: int = 0
    locked_until: datetime | None = None
    last_login_at: datetime | None = None


class UserCredentialCreate(UserCredentialBase):
    pass


class UserCredentialUpdate(BaseModel):
    person_id: UUID | None = None
    provider: AuthProvider | None = None
    username: str | None = Field(default=None, max_length=150)
    password_hash: str | None = Field(default=None, max_length=255)
    must_change_password: bool | None = None
    password_updated_at: datetime | None = None
    failed_login_attempts: int | None = None
    locked_until: datetime | None = None
    last_login_at: datetime | None = None


class UserCredentialRead(UserCredentialBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class MFAMethodBase(BaseModel):
    person_id: UUID
    method_type: MFAMethodType
    label: str | None = Field(default=None, max_length=120)
    secret: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=255)
    is_primary: bool = False
    enabled: bool = True
    verified_at: datetime | None = None
    last_used_at: datetime | None = None


class MFAMethodCreate(MFAMethodBase):
    pass


class MFAMethodUpdate(BaseModel):
    person_id: UUID | None = None
    method_type: MFAMethodType | None = None
    label: str | None = Field(default=None, max_length=120)
    secret: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=255)
    is_primary: bool | None = None
    enabled: bool | None = None
    verified_at: datetime | None = None
    last_used_at: datetime | None = None


class MFAMethodRead(MFAMethodBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class SessionBase(BaseModel):
    person_id: UUID
    status: SessionStatus = SessionStatus.active
    token_hash: str = Field(min_length=1, max_length=255)
    ip_address: str | None = Field(default=None, max_length=64)
    user_agent: str | None = Field(default=None, max_length=255)
    last_seen_at: datetime | None = None
    expires_at: datetime
    revoked_at: datetime | None = None


class SessionCreate(SessionBase):
    pass


class SessionUpdate(BaseModel):
    person_id: UUID | None = None
    status: SessionStatus | None = None
    token_hash: str | None = Field(default=None, max_length=255)
    ip_address: str | None = Field(default=None, max_length=64)
    user_agent: str | None = Field(default=None, max_length=255)
    last_seen_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None


class SessionRead(SessionBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime


class ApiKeyBase(BaseModel):
    person_id: UUID | None = None
    label: str | None = Field(default=None, max_length=120)
    key_hash: str = Field(min_length=1, max_length=255)
    is_active: bool = True
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None


class ApiKeyCreate(ApiKeyBase):
    pass


class ApiKeyGenerateRequest(BaseModel):
    person_id: UUID | None = None
    label: str | None = Field(default=None, max_length=120)
    expires_at: datetime | None = None


class ApiKeyUpdate(BaseModel):
    person_id: UUID | None = None
    label: str | None = Field(default=None, max_length=120)
    key_hash: str | None = Field(default=None, max_length=255)
    is_active: bool | None = None
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None


class ApiKeyRead(ApiKeyBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime

    @field_serializer("key_hash")
    def _mask_key_hash(self, value: str) -> str:
        if not value:
            return ""
        suffix = value[-4:]
        return f"{'*' * max(len(value) - 4, 4)}{suffix}"


class ApiKeyGenerateResponse(BaseModel):
    key: str
    api_key: ApiKeyRead
