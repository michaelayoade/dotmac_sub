"""Wire contract for the vendor (field contractor) JSON authentication API.

These shapes are what ``field_mobile`` already sends and parses
(``lib/features/auth/auth_repository.dart``, ``lib/core/api/api_client.dart``).
They are pinned by ``tests/test_vendor_auth_api_contract.py``; changing a field
name here breaks a shipped mobile build that cannot be redeployed atomically
with the server.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class VendorLoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=150)
    password: str = Field(min_length=1, max_length=255)


class VendorMfaVerifyRequest(BaseModel):
    mfa_token: str = Field(min_length=1)
    code: str = Field(min_length=6, max_length=10)


class VendorRefreshRequest(BaseModel):
    refresh_token: str | None = Field(default=None, min_length=1)


class VendorLogoutRequest(BaseModel):
    refresh_token: str | None = Field(default=None, min_length=1)


class VendorTokenResponse(BaseModel):
    """A completed vendor authentication.

    ``vendor_id`` is the admitted ``FieldVendor`` — the field app stores it to
    scope its local projections. ``refresh_token`` is null when the caller did
    not opt into body delivery (see ``X-Auth-Refresh-In-Body``); it is then set
    as the httpOnly refresh cookie instead.
    """

    access_token: str
    refresh_token: str | None = None
    token_type: str = "bearer"  # noqa: S105 - OAuth token type label, not a credential
    vendor_id: UUID | None = None


class VendorLoginResponse(BaseModel):
    """Login answers with tokens OR with an MFA challenge, never both."""

    access_token: str | None = None
    refresh_token: str | None = None
    token_type: str = "bearer"  # noqa: S105 - OAuth token type label, not a credential
    vendor_id: UUID | None = None
    mfa_required: bool = False
    mfa_token: str | None = None


class VendorLogoutResponse(BaseModel):
    revoked_at: datetime
