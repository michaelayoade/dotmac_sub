"""Typed, read-only browse contracts for saved website enquiries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import IntEnum, StrEnum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.lead_acquisition import WebsiteEnquiryDetails, WebsiteEnquiryStatus


class EnquiryPageSize(IntEnum):
    ten = 10
    twenty_five = 25
    fifty = 50
    hundred = 100


class EnquiryLeadFilter(StrEnum):
    all = "all"
    linked = "linked"
    unlinked = "unlinked"

    @property
    def label(self) -> str:
        return {
            self.all: "All enquiries",
            self.linked: "Linked Lead",
            self.unlinked: "No linked Lead",
        }[self]


class EnquiryReceiptState(StrEnum):
    verified = "verified"
    processing = "processing"
    processed = "processed"
    retryable = "retryable"
    dead_letter = "dead_letter"
    unknown = "unknown"

    @property
    def label(self) -> str:
        return {
            self.verified: "Received",
            self.processing: "Processing",
            self.processed: "Enquiry saved",
            self.retryable: "Retry pending",
            self.dead_letter: "Needs review",
            self.unknown: "Status unavailable",
        }[self]


class WebsiteEnquiryListQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    search: str = Field(default="", max_length=200)
    source: str = Field(default="", max_length=200)
    campaign: str = Field(default="", max_length=200)
    lead_filter: EnquiryLeadFilter = EnquiryLeadFilter.all
    received_from: date | None = None
    received_to: date | None = None
    page: int = Field(default=1, ge=1)
    per_page: EnquiryPageSize = EnquiryPageSize.twenty_five

    @property
    def page_sizes(self) -> tuple[EnquiryPageSize, ...]:
        return tuple(EnquiryPageSize)

    @property
    def lead_filter_options(self) -> tuple[EnquiryLeadFilter, ...]:
        return tuple(EnquiryLeadFilter)

    @property
    def has_advanced_filters(self) -> bool:
        return bool(
            self.source
            or self.campaign
            or self.received_from
            or self.received_to
            or self.lead_filter is not EnquiryLeadFilter.all
            or self.per_page is not EnquiryPageSize.twenty_five
        )

    @model_validator(mode="after")
    def date_order(self) -> Self:
        if (
            self.received_from
            and self.received_to
            and self.received_from > self.received_to
        ):
            raise ValueError("Received-from date must not be after received-to date")
        return self


@dataclass(frozen=True, slots=True)
class WebsiteEnquiryDetailQuery:
    receipt_id: UUID


@dataclass(frozen=True, slots=True)
class WebsiteEnquiryRow:
    receipt_id: UUID
    received_at: datetime
    receipt_state: EnquiryReceiptState
    reference: str | None
    details_status: WebsiteEnquiryStatus
    enquiry: WebsiteEnquiryDetails | None
    linked_lead_id: UUID | None
    linked_lead_title: str | None
    linked_lead_active: bool | None


@dataclass(frozen=True, slots=True)
class WebsiteEnquiryPage:
    query: WebsiteEnquiryListQuery
    items: tuple[WebsiteEnquiryRow, ...]
    total: int
    total_pages: int


class WebsiteReceiptConsequence(BaseModel):
    """Read only the named, processed reference from integration evidence."""

    model_config = ConfigDict(extra="ignore", frozen=True)
    reference: str | None = Field(
        default=None, pattern=r"^(?:FWA-[A-F0-9]{24}|FBR-[A-F0-9]{12})$"
    )
