"""Immutable native field execution commands and queries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from app.services.owner_commands import CommandContext


class FieldTransitionSource(StrEnum):
    geofence = "geofence"
    manual = "manual"


class FieldEvent(StrEnum):
    accept = "accept"
    en_route = "en_route"
    arrived = "arrived"
    start = "start"
    pause = "pause"
    hold = "hold"
    resume = "resume"
    complete = "complete"
    unable_to_complete = "unable_to_complete"


@dataclass(frozen=True, slots=True)
class FieldJobQuery:
    requester_system_user_id: UUID
    public_id: str


@dataclass(frozen=True, slots=True)
class FieldJobsQuery:
    requester_system_user_id: UUID
    status: str | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
    limit: int = 50
    offset: int = 0


@dataclass(frozen=True, slots=True)
class FieldTransitionPayload:
    source: FieldTransitionSource | None = None
    distance_m: float | None = None
    reason: str | None = None
    signature_unavailable_reason: str | None = None
    movement_session_id: UUID | None = None
    destination_type: str | None = None
    destination_id: str | None = None
    destination_label: str | None = None
    destination_latitude: float | None = None
    destination_longitude: float | None = None
    latitude: float | None = None
    longitude: float | None = None
    label: str | None = None
    accuracy_m: float | None = None


@dataclass(frozen=True, slots=True)
class ApplyFieldTransition:
    context: CommandContext
    requester_system_user_id: UUID
    public_id: str
    event: FieldEvent
    client_event_id: UUID
    occurred_at: datetime | None = None
    latitude: float | None = None
    longitude: float | None = None
    note: str | None = None
    payload: FieldTransitionPayload = FieldTransitionPayload()


@dataclass(frozen=True, slots=True)
class FieldWorkLogEntry:
    start_at: datetime
    end_at: datetime | None = None
    notes: str | None = None
    client_ref: UUID | None = None


@dataclass(frozen=True, slots=True)
class SubmitFieldWorkLogs:
    context: CommandContext
    requester_system_user_id: UUID
    public_id: str
    entries: tuple[FieldWorkLogEntry, ...]


@dataclass(frozen=True, slots=True)
class CreateFieldAttachment:
    context: CommandContext
    requester_system_user_id: UUID
    kind: str
    file_name: str
    mime_type: str | None
    content: bytes
    client_ref: UUID | None = None
    public_id: str | None = None
    note_id: UUID | None = None
    latitude: float | None = None
    longitude: float | None = None
    captured_at: datetime | None = None
    signer_name: str | None = None
    asset_type: str | None = None
    asset_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class FieldAttachmentQuery:
    requester_system_user_id: UUID
    public_id: str | None = None
    note_id: UUID | None = None
    kind: str | None = None
    limit: int = 50
    offset: int = 0


@dataclass(frozen=True, slots=True)
class FieldAttachmentIdentity:
    requester_system_user_id: UUID
    attachment_id: UUID


@dataclass(frozen=True, slots=True)
class DeleteFieldAttachment:
    context: CommandContext
    requester_system_user_id: UUID
    attachment_id: UUID


@dataclass(frozen=True, slots=True)
class UpdateFieldJobLocation:
    context: CommandContext
    requester_system_user_id: UUID
    public_id: str
    latitude: float
    longitude: float
