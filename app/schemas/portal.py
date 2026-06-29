"""Schemas for the customer Portal API broker (RFC #73)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class PortalSessionResponse(BaseModel):
    """A brokered, short-lived Portal API token the client uses to call the CRM
    Portal API directly (e.g. Refer & Earn)."""

    portal_token: str
    expires_at: int = Field(..., description="Unix epoch seconds")
    api_base: str = Field(..., description="Absolute base URL for the CRM Portal API")
