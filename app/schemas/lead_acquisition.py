"""Read-only, allowlisted facts for an authorized Lead acquisition view."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID


class WebsiteEnquiryStatus(StrEnum):
    not_recorded = "not_recorded"
    not_linked = "not_linked"
    available = "available"
    unavailable = "unavailable"
    invalid = "invalid"


@dataclass(frozen=True, slots=True)
class LeadAcquisitionQuery:
    lead_id: UUID


@dataclass(frozen=True, slots=True)
class AcquisitionTags:
    source: str | None = None
    medium: str | None = None
    campaign: str | None = None
    content: str | None = None
    term: str | None = None
    campaign_id: str | None = None
    ad_set_id: str | None = None
    ad_id: str | None = None
    click_id: str | None = None
    landing_path: str | None = None


@dataclass(frozen=True, slots=True)
class LeadAcquisitionOrigin:
    origin_id: UUID
    lead_id: UUID
    channel: str
    capture_method: str
    form_version: str | None
    captured_at: datetime
    created_at: datetime
    tags: AcquisitionTags
    receipt_id: UUID | None = None
    journey_id: UUID | None = None
    reference: str | None = None
    submitted_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class WebsiteEnquiryDetails:
    full_name: str
    phone: str | None
    email: str | None
    address: str | None
    area: str | None
    latitude: Decimal | None
    longitude: Decimal | None
    selected_plan: str | None
    message: str | None
    submitted_at: datetime
    captured_at: datetime | None
    tags: AcquisitionTags


@dataclass(frozen=True, slots=True)
class LeadAcquisitionView:
    origin: LeadAcquisitionOrigin | None
    enquiry_status: WebsiteEnquiryStatus
    enquiry: WebsiteEnquiryDetails | None = None
