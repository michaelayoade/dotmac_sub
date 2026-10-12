"""Project-owned explicit field requirements and flush-only completion participants."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.services.owner_commands import CommandContext


@dataclass(frozen=True, slots=True, kw_only=True)
class RegisterProjectFieldCompletionRequirement:
    enrollment_id: UUID
    work_order_id: UUID
    project_id: UUID
    task_id: UUID | None


@dataclass(frozen=True, slots=True)
class ProjectFieldRequirement:
    enrollment_id: UUID
    work_order_id: UUID
    project_id: UUID
    task_id: UUID | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ProjectFieldScopeQuery:
    project_id: UUID
    task_id: UUID | None
    lock: bool = False


@dataclass(frozen=True, slots=True)
class ProjectFieldScope:
    project_id: UUID
    task_id: UUID | None
    title: str
    status: str
    project_revision: datetime
    task_revision: datetime | None
    requirement_token: str
    requirements: tuple[ProjectFieldRequirement, ...]
    task_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class CompleteAcceptedProjectFieldScope:
    context: CommandContext
    application_id: UUID
    project_id: UUID
    task_id: UUID | None
    expected_project_revision: datetime
    expected_task_revision: datetime | None
    expected_requirement_token: str
