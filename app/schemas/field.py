from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class DeviceTokenRegister(BaseModel):
    platform: str = Field(min_length=1, max_length=20)
    fcm_token: str = Field(min_length=1, max_length=512)
    app_version: str | None = Field(default=None, max_length=40)


class DeviceTokenRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    subscriber_id: UUID | None = None
    system_user_id: UUID | None = None
    platform: str | None = None
    app_version: str | None = None
    is_active: bool
    created_at: datetime
    last_seen_at: datetime


class FieldMeResponse(BaseModel):
    person_id: UUID
    name: str
    email: str | None = None
    technician_title: str | None = None
    region: str | None = None
    open_jobs: int
    completed_today: int


class FieldJobSummary(BaseModel):
    """Technician job-list item sourced from the CRM work-order mirror."""

    id: str
    work_order_mirror_id: UUID
    title: str
    description: str | None = None
    status: str
    priority: str | None = None
    work_type: str | None = None
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    estimated_duration_minutes: int | None = None
    estimated_arrival_at: datetime | None = None
    started_at: datetime | None = None
    paused_at: datetime | None = None
    resumed_at: datetime | None = None
    completed_at: datetime | None = None
    total_active_seconds: int | None = None
    technician_name: str | None = None
    technician_phone: str | None = None
    address: str | None = None
    tags: list[str] = Field(default_factory=list)


class FieldCustomer(BaseModel):
    subscriber_id: UUID
    name: str | None = None
    phone: str | None = None
    email: str | None = None
    address_text: str | None = None
    service_plan: str | None = None
    account_number: str | None = None
    status: str | None = None


class FieldJobLocation(BaseModel):
    latitude: float | None = None
    longitude: float | None = None
    address_text: str | None = None
    source: str


class FieldJobDestination(BaseModel):
    destination_type: str = Field(min_length=1, max_length=40)
    destination_id: str | None = Field(default=None, max_length=120)
    label: str = Field(min_length=1, max_length=255)
    latitude: float | None = None
    longitude: float | None = None
    address_text: str | None = None


class FieldJobDestinationsResponse(BaseModel):
    items: list[FieldJobDestination]
    count: int


class FieldSiteContact(BaseModel):
    name: str | None = None
    phone: str | None = None
    email: str | None = None
    relationship: str | None = None


class FieldVisitHistoryItem(BaseModel):
    work_order_id: str
    title: str
    work_type: str | None = None
    status: str | None = None
    completed_at: datetime | None = None


class FieldOpenTicketItem(BaseModel):
    id: str
    ref: str
    subject: str | None = None
    status: str | None = None


class FieldJobHistoryItem(BaseModel):
    id: str
    type: str
    title: str
    description: str | None = None
    occurred_at: datetime | None = None
    actor_name: str | None = None
    status: str | None = None
    is_internal: bool | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class FieldJobDetail(BaseModel):
    job: FieldJobSummary
    customer: FieldCustomer | None = None
    location: FieldJobLocation
    ticket_ref: str | None = None
    project_id: str | None = None
    access_notes: str | None = None
    additional_contacts: list[FieldSiteContact] = Field(default_factory=list)
    recent_visits: list[FieldVisitHistoryItem] = Field(default_factory=list)
    open_tickets: list[FieldOpenTicketItem] = Field(default_factory=list)
    notes: list[dict[str, Any]] = Field(default_factory=list)
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    materials: list[dict[str, Any]] = Field(default_factory=list)
    material_requests: list[dict[str, Any]] = Field(default_factory=list)
    worklogs: list[dict[str, Any]] = Field(default_factory=list)
    history: list[FieldJobHistoryItem] = Field(default_factory=list)


class FieldScheduleEntry(BaseModel):
    type: str
    start_at: datetime
    end_at: datetime | None = None
    title: str
    reference_id: str


class FieldMapAsset(BaseModel):
    id: UUID
    type: str
    title: str
    subtitle: str | None = None
    latitude: float
    longitude: float
    status: str | None = None
    updated_at: datetime | None = None
    distance_m: float | None = None
