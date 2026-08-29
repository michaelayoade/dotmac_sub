"""Wire types for the field-mobile OIDC federation endpoints.

Both request models set ``extra="forbid"``. That is not tidiness: it is the
enforcement of "Sub never receives the PKCE verifier". A client that sends
``code_verifier`` — by mistake, by copying an OAuth example, or because a
future refactor added it — gets a 422 instead of silently handing Sub a secret
it must not hold and has nowhere to put. The guarantee is a schema, not a
review comment.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class OidcMobileStartRequest(BaseModel):
    """Ask for the parameters of one ceremony."""

    model_config = ConfigDict(extra="forbid")

    #: The PKCE method the device will use. Only ``S256`` is accepted. The
    #: CHALLENGE is sent to the identity provider by the device and the
    #: VERIFIER never leaves it; neither appears in this model.
    code_challenge_method: str = Field(default="S256", max_length=16)


class OidcMobileStartResponse(BaseModel):
    """The ceremony's parameters. ``nonce`` is shown once and never stored."""

    ceremony_id: UUID
    issuer: str
    client_id: str
    redirect_uri: str
    audience: str
    scope: str
    nonce: str
    code_challenge_method: str
    expires_at: datetime
    expires_in_seconds: int


class OidcMobileExchangeRequest(BaseModel):
    """Redeem one ceremony with one identity-provider assertion."""

    model_config = ConfigDict(extra="forbid")

    ceremony_id: UUID
    #: The identity provider's ID token. Sub never receives the authorization
    #: code that produced it, nor the PKCE verifier that redeemed it.
    id_token: str = Field(min_length=1, max_length=8192)
    #: What the device actually used. Compared for EXACT equality against what
    #: the ceremony pinned — a mismatch is refused, never reconciled.
    client_id: str = Field(min_length=1, max_length=255)
    redirect_uri: str = Field(min_length=1, max_length=1024)


class OidcMobileExchangeResponse(BaseModel):
    """The normal Sub pair. Identical in shape to every other login response."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"  # noqa: S105 - OAuth token type label
    principal_type: str
    principal_id: UUID
    ceremony_id: UUID


class OidcRefusalDetail(BaseModel):
    """A safe refusal category. Never names which check failed about WHOM."""

    code: str
    reason: str


class OidcRefusalResponse(BaseModel):
    detail: OidcRefusalDetail
