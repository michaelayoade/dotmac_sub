from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
)

from app.models.auth import AuthProvider, MFAMethodType, SessionStatus


class UserCredentialFields(BaseModel):
    """Fields shared by the credential write and read shapes."""

    provider: AuthProvider = AuthProvider.local
    username: str | None = Field(default=None, max_length=150)
    radius_server_id: UUID | None = None
    must_change_password: bool = False
    password_updated_at: datetime | None = None
    failed_login_attempts: int = 0
    locked_until: datetime | None = None
    last_login_at: datetime | None = None
    is_active: bool = True


class UserCredentialCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    subscriber_id: UUID = Field(
        validation_alias=AliasChoices("subscriber_id", "person_id")
    )
    provider: AuthProvider | None = None
    username: str | None = Field(default=None, max_length=150)
    radius_server_id: UUID | None = None
    password: str | None = Field(default=None, max_length=255, repr=False)


class UserCredentialUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    username: str | None = Field(default=None, max_length=150)
    radius_server_id: UUID | None = None
    password: str | None = Field(default=None, max_length=255, repr=False)


class UserCredentialRead(UserCredentialFields):
    model_config = ConfigDict(from_attributes=True)

    # A row binds exactly one principal, so staff-owned rows carry
    # system_user_id and a NULL subscriber_id. Reads must accept that;
    # writes keep it required, which is why the read shape does not
    # inherit the write shape.
    subscriber_id: UUID | None = None

    id: UUID
    created_at: datetime
    updated_at: datetime


class MFAMethodFields(BaseModel):
    """Public MFA-method read fields; enrollment has its own auth contract."""

    method_type: MFAMethodType
    label: str | None = Field(default=None, max_length=120)
    phone: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=255)
    is_primary: bool = False
    enabled: bool = True
    is_active: bool = True
    verified_at: datetime | None = None
    last_used_at: datetime | None = None


class MFAMethodRead(MFAMethodFields):
    model_config = ConfigDict(from_attributes=True)

    # A row binds exactly one principal, so staff-owned rows carry
    # system_user_id and a NULL subscriber_id. Reads must accept both.
    subscriber_id: UUID | None = None

    id: UUID
    created_at: datetime
    updated_at: datetime


class SessionFields(BaseModel):
    """Administrative session read fields; authentication owns issuance."""

    status: SessionStatus = SessionStatus.active
    token_hash: str = Field(min_length=1, max_length=255)
    ip_address: str | None = Field(default=None, max_length=64)
    user_agent: str | None = Field(default=None, max_length=512)
    last_seen_at: datetime | None = None
    expires_at: datetime
    revoked_at: datetime | None = None


class SessionRead(SessionFields):
    model_config = ConfigDict(from_attributes=True)

    # A row binds exactly one principal, so staff-owned rows carry
    # system_user_id and a NULL subscriber_id. Reads must accept both.
    subscriber_id: UUID | None = None

    id: UUID
    created_at: datetime


class ApiKeyBase(BaseModel):
    subscriber_id: UUID | None = Field(
        default=None, validation_alias=AliasChoices("subscriber_id", "person_id")
    )
    label: str | None = Field(default=None, max_length=120)
    key_hash: str = Field(min_length=1, max_length=255)
    scopes: list[str] = Field(default_factory=list)
    is_active: bool = True
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None


class ApiKeyCreate(ApiKeyBase):
    pass


class ApiKeyGenerateRequest(BaseModel):
    subscriber_id: UUID | None = Field(
        default=None, validation_alias=AliasChoices("subscriber_id", "person_id")
    )
    label: str | None = Field(default=None, max_length=120)
    scopes: list[str] = Field(default_factory=list)
    expires_at: datetime | None = None


class ApiKeyUpdate(BaseModel):
    subscriber_id: UUID | None = None
    label: str | None = Field(default=None, max_length=120)
    key_hash: str | None = Field(default=None, min_length=1, max_length=255)
    scopes: list[str] | None = None
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
