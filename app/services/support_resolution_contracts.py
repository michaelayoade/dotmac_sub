"""Typed support-owned customer confirmation intent; never customer consent."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from app.services.action_readiness import ActionReadiness
from app.services.owner_commands import CommandContext
from app.services.permission_admission import PermissionAdmission
from app.services.topology.outage_completion_contracts import (
    ComplaintResolutionScope,
    ReviewedComplaintResolutionScope,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class SupportResolutionRequestCommand:
    context: CommandContext
    ticket_id: UUID
    actor_id: UUID | None = None
    grace_hours: int = 24
    access: PermissionAdmission | None = None
    reviewed_shared_scopes: tuple[ReviewedComplaintResolutionScope, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class SupportResolutionPreviewQuery:
    ticket_id: UUID
    access: PermissionAdmission


@dataclass(frozen=True, slots=True, kw_only=True)
class SupportResolutionPreview:
    ticket_id: UUID
    number: str | None
    title: str
    status: str
    readiness: ActionReadiness
    current_request_id: UUID | None
    requested_at: datetime | None
    deadline: datetime | None
    shared_scopes: tuple[ComplaintResolutionScope, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class SupportResolutionRequestOutcome:
    request_id: UUID
    ticket_id: UUID
    requested_at: datetime
    deadline: datetime
    status: Literal["pending_confirmation"]
    replayed: bool
    token_expires_at: datetime | None = None
