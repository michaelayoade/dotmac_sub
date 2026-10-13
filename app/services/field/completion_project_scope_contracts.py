"""Explicit staff action: apply accepted physical evidence to project lifecycle."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.services.action_readiness import ActionReadiness
from app.services.owner_commands import CommandContext
from app.services.permission_admission import PermissionAdmission
from app.services.project_field_completion_contracts import ProjectFieldRequirement

CONCERN = "accepted project field scope completion"
SCOPE = "project:accepted-field-completion"


@dataclass(frozen=True, slots=True, kw_only=True)
class ApplyAcceptedProjectCompletion:
    context: CommandContext
    access: PermissionAdmission
    project_id: UUID
    task_id: UUID | None
    expected_project_revision: datetime
    expected_task_revision: datetime | None
    expected_requirement_token: str


@dataclass(frozen=True, slots=True, kw_only=True)
class AcceptedProjectCompletionPreviewQuery:
    access: PermissionAdmission
    project_id: UUID
    task_id: UUID | None


@dataclass(frozen=True, slots=True)
class ProjectFieldWorkOrderLabel:
    public_id: str
    title: str
    status: str


@dataclass(frozen=True, slots=True)
class AcceptedProjectCompletionPreview:
    project_id: UUID
    task_id: UUID | None
    title: str
    status: str
    project_revision: datetime
    task_revision: datetime | None
    requirement_token: str
    requirements: tuple[ProjectFieldRequirement, ...]
    readiness: ActionReadiness
    work_orders: tuple[ProjectFieldWorkOrderLabel, ...] = ()


@dataclass(frozen=True, slots=True)
class AcceptedProjectCompletionOutcome:
    application_id: UUID
    project_id: UUID
    task_id: UUID | None
    status: str
    replayed: bool = False
