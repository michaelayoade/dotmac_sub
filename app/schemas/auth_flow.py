from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

from app.models.auth import AuthProvider


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=150)
    password: str = Field(min_length=1, max_length=255)
    provider: AuthProvider | None = None


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str | None = None
    token_type: str = "bearer"


class LoginResponse(BaseModel):
    access_token: str | None = None
    refresh_token: str | None = None
    token_type: str = "bearer"
    mfa_required: bool = False
    mfa_token: str | None = None


class MfaSetupRequest(BaseModel):
    person_id: UUID
    label: str | None = Field(default=None, max_length=120)


class MfaSetupResponse(BaseModel):
    method_id: UUID
    secret: str
    otpauth_uri: str


class MfaConfirmRequest(BaseModel):
    method_id: UUID
    code: str = Field(min_length=6, max_length=10)


class MfaVerifyRequest(BaseModel):
    mfa_token: str = Field(min_length=1)
    code: str = Field(min_length=6, max_length=10)


class RefreshRequest(BaseModel):
    refresh_token: str | None = Field(default=None, min_length=1)


class LogoutRequest(BaseModel):
    refresh_token: str | None = Field(default=None, min_length=1)


class LogoutResponse(BaseModel):
    revoked_at: datetime


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    detail: ErrorDetail


class MeResponse(BaseModel):
    id: UUID
    first_name: str
    last_name: str
    display_name: str | None = None
    avatar_url: str | None = None
    email: EmailStr
    email_verified: bool = False
    phone: str | None = None
    date_of_birth: date | None = None
    gender: str = "unknown"
    preferred_contact_method: str | None = None
    locale: str | None = None
    timezone: str | None = None
    roles: list[str] = Field(default_factory=list)
    scopes: list[str] = Field(default_factory=list)


class MeUpdateRequest(BaseModel):
    first_name: str | None = Field(default=None, max_length=80)
    last_name: str | None = Field(default=None, max_length=80)
    display_name: str | None = Field(default=None, max_length=120)
    phone: str | None = Field(default=None, max_length=40)
    date_of_birth: date | None = None
    gender: str | None = None
    preferred_contact_method: str | None = None
    locale: str | None = Field(default=None, max_length=16)
    timezone: str | None = Field(default=None, max_length=64)


class AvatarUploadResponse(BaseModel):
    avatar_url: str


class SessionInfoResponse(BaseModel):
    id: UUID
    status: str
    ip_address: str | None = None
    user_agent: str | None = None
    created_at: datetime
    last_seen_at: datetime | None = None
    expires_at: datetime
    is_current: bool = False


class SessionListResponse(BaseModel):
    sessions: list[SessionInfoResponse]
    total: int


class SessionRevokeResponse(BaseModel):
    revoked_at: datetime
    revoked_count: int = 1


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=255)
    new_password: str = Field(min_length=8, max_length=255)


class PasswordChangeResponse(BaseModel):
    changed_at: datetime


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ForgotPasswordResponse(BaseModel):
    message: str = "If the email exists, a reset link has been sent"


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=1)
    new_password: str = Field(min_length=8, max_length=255)


class ResetPasswordResponse(BaseModel):
    reset_at: datetime
