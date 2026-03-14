from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.support import TicketChannel, TicketPriority, TicketStatus


class AttachmentMeta(BaseModel):
    file_name: str
    content_type: str
    file_size: int
    storage_key: str


class TicketBase(BaseModel):
    subscriber_id: UUID | None = None
    customer_account_id: UUID | None = None
    lead_id: UUID | None = None
    customer_person_id: UUID | None = None
    created_by_person_id: UUID | None = None
    assigned_to_person_id: UUID | None = None
    technician_person_id: UUID | None = None
    ticket_manager_person_id: UUID | None = None
    site_coordinator_person_id: UUID | None = None
    service_team_id: UUID | None = None

    title: str = Field(min_length=1, max_length=255)
    description: str | None = None
    region: str | None = Field(default=None, max_length=80)
    status: TicketStatus | None = None
    priority: TicketPriority = TicketPriority.normal
    ticket_type: str | None = Field(default=None, max_length=80)
    channel: TicketChannel = TicketChannel.web
    tags: list[str] = Field(default_factory=list)
    metadata_: dict | None = Field(default=None, validation_alias="metadata", serialization_alias="metadata")

    due_at: datetime | None = None
    resolved_at: datetime | None = None
    closed_at: datetime | None = None

    assignee_person_ids: list[UUID] = Field(default_factory=list)
    related_outage_ticket_id: UUID | None = None


class TicketCreate(TicketBase):
    pass


class TicketUpdate(BaseModel):
    subscriber_id: UUID | None = None
    customer_account_id: UUID | None = None
    lead_id: UUID | None = None
    customer_person_id: UUID | None = None
    created_by_person_id: UUID | None = None
    assigned_to_person_id: UUID | None = None
    technician_person_id: UUID | None = None
    ticket_manager_person_id: UUID | None = None
    site_coordinator_person_id: UUID | None = None
    service_team_id: UUID | None = None

    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    region: str | None = Field(default=None, max_length=80)
    status: TicketStatus | None = None
    priority: TicketPriority | None = None
    ticket_type: str | None = Field(default=None, max_length=80)
    channel: TicketChannel | None = None
    tags: list[str] | None = None
    metadata_: dict | None = Field(default=None, validation_alias="metadata", serialization_alias="metadata")

    due_at: datetime | None = None
    resolved_at: datetime | None = None
    closed_at: datetime | None = None

    assignee_person_ids: list[UUID] | None = None


class TicketRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    number: str | None

    subscriber_id: UUID | None
    customer_account_id: UUID | None
    lead_id: UUID | None
    customer_person_id: UUID | None
    created_by_person_id: UUID | None
    assigned_to_person_id: UUID | None
    technician_person_id: UUID | None
    ticket_manager_person_id: UUID | None
    site_coordinator_person_id: UUID | None
    service_team_id: UUID | None

    title: str
    description: str | None
    region: str | None
    status: TicketStatus
    priority: TicketPriority
    ticket_type: str | None
    channel: TicketChannel
    tags: list[str] | None = None
    metadata_: dict | None = Field(default=None, validation_alias="metadata", serialization_alias="metadata")
    attachments: list[dict] | None = None

    due_at: datetime | None
    resolved_at: datetime | None
    closed_at: datetime | None

    merged_into_ticket_id: UUID | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class TicketBulkUpdateItem(BaseModel):
    ticket_id: UUID
    status: TicketStatus | None = None
    priority: TicketPriority | None = None
    assigned_to_person_id: UUID | None = None


class TicketBulkUpdateRequest(BaseModel):
    items: list[TicketBulkUpdateItem]


class TicketCommentBase(BaseModel):
    body: str = Field(min_length=1)
    is_internal: bool = False
    attachments: list[AttachmentMeta] = Field(default_factory=list)


class TicketCommentCreate(TicketCommentBase):
    author_person_id: UUID | None = None


class TicketCommentUpdate(BaseModel):
    body: str | None = Field(default=None, min_length=1)
    is_internal: bool | None = None
    attachments: list[AttachmentMeta] | None = None


class TicketCommentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    ticket_id: UUID
    author_person_id: UUID | None
    body: str
    is_internal: bool
    attachments: list[dict] | None = None
    created_at: datetime


class TicketSlaEventBase(BaseModel):
    event_type: str = Field(min_length=1, max_length=80)
    expected_at: datetime | None = None
    actual_at: datetime | None = None
    metadata_: dict | None = Field(default=None, validation_alias="metadata", serialization_alias="metadata")


class TicketSlaEventCreate(TicketSlaEventBase):
    ticket_id: UUID


class TicketSlaEventUpdate(BaseModel):
    event_type: str | None = Field(default=None, min_length=1, max_length=80)
    expected_at: datetime | None = None
    actual_at: datetime | None = None
    metadata_: dict | None = Field(default=None, validation_alias="metadata", serialization_alias="metadata")


class TicketSlaEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    ticket_id: UUID
    event_type: str
    expected_at: datetime | None
    actual_at: datetime | None
    metadata_: dict | None = Field(default=None, validation_alias="metadata", serialization_alias="metadata")
    created_at: datetime


class TicketLinkCreate(BaseModel):
    to_ticket_id: UUID
    link_type: str = Field(min_length=1, max_length=80)


class TicketMergeRequest(BaseModel):
    target_ticket_id: UUID
    reason: str | None = None


class TicketLookupQuery(BaseModel):
    ticket: str

    @field_validator("ticket")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        text = v.strip()
        if not text:
            raise ValueError("ticket cannot be empty")
        return text
