from __future__ import annotations

from pydantic import BaseModel, Field


class StatusTransitionRequest(BaseModel):
    """Request payload for transitioning entity status."""
    to_status: str = Field(min_length=1, max_length=40)
    note: str | None = None
