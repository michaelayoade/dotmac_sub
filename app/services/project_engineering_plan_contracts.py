"""Native engineering plans: immutable content, explicit decisions, no issuance."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.infrastructure import InfrastructureReference
from app.services.action_readiness import ActionReadiness
from app.services.owner_commands import CommandContext
from app.services.permission_admission import PermissionAdmission

OWNER = "operations.project_engineering_plans"
CONCERN = "native project engineering plan approval lifecycle"
SCOPE = "project:engineering_plan"
READ = SCOPE + ":read"
WRITE = SCOPE + ":write"
APPROVE = SCOPE + ":approve"
REVOKE = SCOPE + ":revoke"
POLICY = SCOPE + ":policy"


class EngineeringTechnology(StrEnum):
    fiber = "fiber"
    radio = "radio"


class EngineeringPlanAction(StrEnum):
    drafted = "drafted"
    policy_published = "policy_published"
    approve = "approve"
    reject = "reject"
    revoke = "revoke"


class PlanDecisionKind(StrEnum):
    approve = "approve"
    reject = "reject"
    revoke = "revoke"
    supersede = "supersede"


class GeoPoint(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    latitude: Decimal = Field(ge=-90, le=90, allow_inf_nan=False)
    longitude: Decimal = Field(ge=-180, le=180, allow_inf_nan=False)


class SurveyedPlanLocation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["surveyed"] = "surveyed"
    description: str = Field(min_length=1, max_length=2000)
    point: GeoPoint


class PopSitePlanLocation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["pop_site"] = "pop_site"
    pop_site_id: UUID


PlanLocation = Annotated[
    SurveyedPlanLocation | PopSitePlanLocation, Field(discriminator="kind")
]


class EngineeringCriterion(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: UUID
    order: int = Field(ge=0)
    statement: str = Field(min_length=1, max_length=4000)
    verification_method: str = Field(min_length=1, max_length=2000)


class EngineeringPlanContent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    technology: EngineeringTechnology
    infrastructure: InfrastructureReference
    location: PlanLocation
    scope_statement: str = Field(min_length=1, max_length=8000)
    criteria: tuple[EngineeringCriterion, ...] = Field(min_length=1, max_length=100)


@dataclass(frozen=True, slots=True, kw_only=True)
class PlanCommand:
    context: CommandContext
    access: PermissionAdmission


@dataclass(frozen=True, slots=True, kw_only=True)
class PublishApprovalPolicy(PlanCommand):
    project_id: UUID
    expected_project_updated_at: datetime
    expected_generation: int
    independent_reviewer_required: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class DraftEngineeringPlan(PlanCommand):
    plan_id: UUID
    project_id: UUID
    task_id: UUID | None
    expected_project_updated_at: datetime
    expected_task_updated_at: datetime | None
    expected_generation: int
    content: EngineeringPlanContent


@dataclass(frozen=True, slots=True, kw_only=True)
class DecideEngineeringPlan(PlanCommand):
    plan_id: UUID
    project_id: UUID
    task_id: UUID | None
    expected_project_updated_at: datetime
    expected_task_updated_at: datetime | None
    expected_generation: int
    revision_id: UUID
    expected_digest: str
    expected_policy_revision_id: UUID
    decision: PlanDecisionKind


@dataclass(frozen=True, slots=True, kw_only=True)
class EngineeringPlanOutcome:
    plan_id: UUID | None
    revision_id: UUID | None
    decision_id: UUID | None
    policy_revision_id: UUID | None
    generation: int
    action: EngineeringPlanAction
    digest: str | None
    replayed: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class CurrentEngineeringPlanQuery:
    access: PermissionAdmission
    plan_id: UUID
    project_id: UUID
    task_id: UUID | None
    expected_revision_id: UUID
    expected_approval_id: UUID
    expected_digest: str


@dataclass(frozen=True, slots=True, kw_only=True)
class CurrentEngineeringPlanApproval:
    plan_id: UUID
    project_id: UUID
    task_id: UUID | None
    revision_id: UUID
    approval_id: UUID
    policy_revision_id: UUID
    digest: str
    content: EngineeringPlanContent
    author_person_id: UUID
    reviewer_person_id: UUID
    generation: int


@dataclass(frozen=True, slots=True, kw_only=True)
class IssuanceEngineeringPlanQuery(CurrentEngineeringPlanQuery):
    """Exact current approval only, for freshly authorized staff dispatch."""


@dataclass(frozen=True, slots=True, kw_only=True)
class CompletionEngineeringPlanQuery(CurrentEngineeringPlanQuery):
    """Exact issued approval freshness for staff completion review."""


class EngineeringReadPurpose(StrEnum):
    overview = "overview"
    policy = "policy"
    draft = "draft"
    review = "review"
    revoke = "revoke"


@dataclass(frozen=True, slots=True, kw_only=True)
class EngineeringWorkspaceQuery:
    access: PermissionAdmission
    project_id: UUID
    task_id: UUID | None
    technology: EngineeringTechnology
    purpose: EngineeringReadPurpose
    history_offset: int = 0
    selected_revision_id: UUID | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class EngineeringPolicyView:
    id: UUID
    revision: int
    independent_reviewer_required: bool
    reason: str
    created_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class EngineeringRevisionView:
    id: UUID
    revision: int
    digest: str
    author_person_id: UUID
    created_at: datetime
    content: EngineeringPlanContent


@dataclass(frozen=True, slots=True, kw_only=True)
class EngineeringDecisionView:
    id: UUID
    revision_id: UUID
    kind: PlanDecisionKind
    actor_person_id: UUID
    reason: str
    created_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class EngineeringWorkspace:
    project_id: UUID
    task_id: UUID | None
    technology: EngineeringTechnology
    purpose: EngineeringReadPurpose
    project_updated_at: datetime
    task_updated_at: datetime | None
    infrastructure: InfrastructureReference | None
    plan_id: UUID | None
    generation: int
    policy_generation: int
    policy: EngineeringPolicyView | None
    revision: EngineeringRevisionView | None
    approval_id: UUID | None
    history: tuple[EngineeringRevisionView, ...]
    decisions: tuple[EngineeringDecisionView, ...]
    history_offset: int
    history_has_more: bool
    readiness: "ActionReadiness"
    evaluated_at: datetime

    project_label: str = "Project"
    task_label: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ProjectDispatchPreviewQuery:
    access: PermissionAdmission
    project_id: UUID
    task_id: UUID | None
    technology: EngineeringTechnology


@dataclass(frozen=True, slots=True, kw_only=True)
class ProjectDispatchPreview:
    project_id: UUID
    task_id: UUID | None
    technology: EngineeringTechnology
    project_updated_at: datetime
    task_updated_at: datetime | None
    approval: CurrentEngineeringPlanApproval | None
    readiness: "ActionReadiness"
    evaluated_at: datetime

    project_label: str = "Project"
    task_label: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class SupportCompletionEngineeringQuery:
    plan_id: UUID
    project_id: UUID
    task_id: UUID | None
    expected_revision_id: UUID
    expected_approval_id: UUID
    expected_digest: str


@dataclass(frozen=True, slots=True, kw_only=True)
class SupportCompletionEngineeringBatchQuery:
    plans: tuple[SupportCompletionEngineeringQuery, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class SupportCompletionEngineeringPreviewBatchQuery(
    SupportCompletionEngineeringBatchQuery
):
    access: PermissionAdmission


class AcceptedProjectCompletionPurpose(StrEnum):
    project = "project"
    task = "task"


@dataclass(frozen=True, slots=True, kw_only=True)
class AcceptedProjectCompletionEngineeringPreviewBatchQuery(
    SupportCompletionEngineeringBatchQuery
):
    access: PermissionAdmission
    purpose: AcceptedProjectCompletionPurpose
