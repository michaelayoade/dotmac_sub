"""Typed restricted identity commands and deliberately minimal customer card."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.models.field_identity import IdentityState
from app.services.file_storage import PreparedFileUpload
from app.services.owner_commands import CommandContext

REVIEW_PERMISSION = "operations:technician:write"
WRITE_SCOPE = "field:identity:write"


@dataclass(frozen=True, slots=True)
class IdentityAccess:
    system_user_id: UUID
    permission_keys: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class SubmitIdentity:
    context: CommandContext
    access: IdentityAccess
    subject_id: UUID
    expected_revision: int
    display_name: str
    company_name: str
    portrait: PreparedFileUpload
    evidence: PreparedFileUpload


@dataclass(frozen=True, slots=True)
class ReviewIdentity:
    context: CommandContext
    access: IdentityAccess
    subject_id: UUID
    revision: int
    expected_version: int
    decision: IdentityState


@dataclass(frozen=True, slots=True)
class IdentityOutcome:
    subject_id: UUID
    revision: int
    version: int
    state: IdentityState


@dataclass(frozen=True, slots=True)
class CustomerIdentityCard:
    subject_id: UUID
    revision: int
    display_name: str
    company_name: str
    verified: bool
    verification_state: IdentityState
    reviewed_at: datetime
    portrait_file_id: UUID


@dataclass(frozen=True, slots=True)
class IdentityDetail:
    outcome: IdentityOutcome
    display_name: str
    company_name: str
    portrait_file_id: UUID
    evidence_file_id: UUID
    review_reason: str | None
