"""Typed provenance for a signed website enquiry followed by WhatsApp."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.fiber_inquiry import FiberAttribution


class WebsiteHandoffAttribution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    form_version: Literal["fiber-whatsapp-v1"] = "fiber-whatsapp-v1"
    observation_id: UUID
    integration_inbox_id: UUID
    reference: str = Field(pattern=r"^FWA-[A-F0-9]{24}$")
    submitted_at: datetime
    attribution: FiberAttribution
