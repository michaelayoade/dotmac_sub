"""Accountable failed-visit followup contracts; no appointment or job issuance."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from app.services.action_readiness import ActionReadiness
from app.services.owner_commands import CommandContext
from app.services.permission_admission import PermissionAdmission

OWNER = "operations.field_visit_followup"
CONCERN = "accountable failed visit followup decisions"
READ = "operations:dispatch:read"
WRITE = "operations:dispatch:assign"


class FailureSourceKind(StrEnum):
    unable_event = "unable_event"
    rejected_decision = "rejected_decision"


class FollowupDisposition(StrEnum):
    triage = "triage"
    revisit_required = "revisit_required"
    no_revisit = "no_revisit"


@dataclass(frozen=True, slots=True)
class FailureSource:
    kind: FailureSourceKind
    source_id: UUID


@dataclass(frozen=True, slots=True)
class FollowupResponsibility:
    team_id: UUID
    system_user_id: UUID | None
    due_at: datetime


@dataclass(frozen=True, slots=True)
class OpenVisitFollowup:
    context: CommandContext
    access: PermissionAdmission
    source: FailureSource
    responsibility: FollowupResponsibility
    reason: str
    completion_read_access: PermissionAdmission | None = None


@dataclass(frozen=True, slots=True)
class DecideVisitFollowup:
    context: CommandContext
    access: PermissionAdmission
    obligation_id: UUID
    expected_revision_id: UUID
    responsibility: FollowupResponsibility
    disposition: FollowupDisposition
    reason: str
    replacement_decision_id: UUID | None = None
    completion_read_access: PermissionAdmission | None = None
    replacement_access: PermissionAdmission | None = None
    expected_replacement_generation: int | None = None


@dataclass(frozen=True, slots=True)
class VisitFollowupOutcome:
    obligation_id: UUID
    work_order_id: UUID
    source: FailureSource
    revision_id: UUID
    sequence: int
    disposition: FollowupDisposition
    responsible_team_id: UUID
    responsible_system_user_id: UUID | None
    responsible_person_id: UUID | None
    due_at: datetime
    replacement_decision_id: UUID | None
    replayed: bool


@dataclass(frozen=True, slots=True)
class FailedVisitIntakeQuery:
    access: PermissionAdmission
    completion_access: PermissionAdmission
    after_work_order_id: UUID | None = None
    limit: int = 30


@dataclass(frozen=True, slots=True)
class PendingFailureSource:
    source: FailureSource
    work_order_id: UUID
    work_order_public_id: str
    work_order_label: str
    occurred_at: datetime
    failure_reason: str
    readiness: ActionReadiness


@dataclass(frozen=True, slots=True)
class FailedVisitIntake:
    rows: tuple[PendingFailureSource, ...]
    next_work_order_id: UUID | None


@dataclass(frozen=True, slots=True)
class VisitFollowupWorkspaceQuery:
    access: PermissionAdmission
    completion_access: PermissionAdmission
    obligation_id: UUID


@dataclass(frozen=True, slots=True)
class VisitFollowupRevisionView:
    revision_id: UUID
    sequence: int
    disposition: FollowupDisposition
    responsible_team_id: UUID
    responsible_team_label: str
    responsible_system_user_id: UUID | None
    responsible_person_id: UUID | None
    responsible_person_label: str | None
    due_at: datetime
    reason: str
    replacement_decision_id: UUID | None
    decided_at: datetime


@dataclass(frozen=True, slots=True)
class VisitFollowupWorkspace:
    obligation_id: UUID
    work_order_id: UUID
    work_order_public_id: str
    source: FailureSource
    revisions: tuple[VisitFollowupRevisionView, ...]
    readiness: ActionReadiness
    replacement_readiness: ActionReadiness


@dataclass(frozen=True, slots=True)
class VisitFollowupRecordedEvent:
    obligation_id: UUID
    revision_id: UUID
    work_order_id: UUID
    sequence: int
    disposition: FollowupDisposition
    source: FailureSource

    def payload(self) -> dict[str, str | int]:
        return {
            "trigger": "operations.field_visit_followup.recorded.v1",
            "schema_version": 1,
            "obligation_id": str(self.obligation_id),
            "revision_id": str(self.revision_id),
            "work_order_id": str(self.work_order_id),
            "sequence": self.sequence,
            "disposition": self.disposition.value,
            "source_kind": self.source.kind.value,
            "source_id": str(self.source.source_id),
        }


@dataclass(frozen=True, slots=True)
class FollowupResponsibilityOptionsQuery:
    access: PermissionAdmission
    completion_access: PermissionAdmission
    team_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class FollowupTeamOption:
    team_id: UUID
    label: str


@dataclass(frozen=True, slots=True)
class FollowupPersonOption:
    system_user_id: UUID
    person_id: UUID
    label: str


@dataclass(frozen=True, slots=True)
class FollowupResponsibilityOptions:
    teams: tuple[FollowupTeamOption, ...]
    people: tuple[FollowupPersonOption, ...]


@dataclass(frozen=True, slots=True)
class VisitFollowupQueueQuery:
    access: PermissionAdmission
    completion_access: PermissionAdmission
    after_obligation_id: UUID | None = None
    limit: int = 30
    include_closed: bool = False


@dataclass(frozen=True, slots=True)
class VisitFollowupQueueRow:
    obligation_id: UUID
    work_order_id: UUID
    work_order_label: str
    disposition: FollowupDisposition
    responsible_team_label: str
    responsible_person_label: str | None
    due_at: datetime
    replacement_decision_id: UUID | None


@dataclass(frozen=True, slots=True)
class VisitFollowupQueue:
    rows: tuple[VisitFollowupQueueRow, ...]
    next_obligation_id: UUID | None


@dataclass(frozen=True, slots=True)
class ApprovedFollowupReplacementQuery:
    access: PermissionAdmission
    completion_access: PermissionAdmission
    replacement_access: PermissionAdmission
    obligation_id: UUID


@dataclass(frozen=True, slots=True)
class ApprovedFollowupReplacementOption:
    decision_id: UUID
    generation: int
    work_order_public_id: str
    work_order_title: str


@dataclass(frozen=True, slots=True)
class FailedVisitSourceQuery:
    access: PermissionAdmission
    completion_access: PermissionAdmission
    source: FailureSource
