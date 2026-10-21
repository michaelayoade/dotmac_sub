"""Shared immutable staff API/web request contracts; admission is server-produced."""

from decimal import Decimal
from enum import StrEnum
from typing import Literal
from uuid import UUID, uuid5

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.schemas.infrastructure import InfrastructureReference, InfrastructureType
from app.services.owner_commands import CommandContext
from app.services.permission_admission import PermissionAdmission
from app.services.project_engineering_plan_contracts import (
    SCOPE,
    DecideEngineeringPlan,
    DraftEngineeringPlan,
    EngineeringCriterion,
    EngineeringPlanContent,
    EngineeringWorkspace,
    GeoPoint,
    PlanDecisionKind,
    PlanLocation,
    PopSitePlanLocation,
    ProjectDispatchPreview,
    PublishApprovalPolicy,
    SurveyedPlanLocation,
)
from app.services.project_work_order_handoff_contracts import (
    PERMISSION,
    IssueProjectWorkOrder,
)
from app.services.work_order_creation_values import WorkOrderCreationPriority
from app.services.work_order_templates import (
    ExplicitWorkOrderClassification,
    WorkOrderAccessTechnology,
    WorkOrderPurpose,
)


class EngineeringIntent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    confirmed: Literal[True]
    intent_id: UUID
    reason: str = Field(min_length=1, max_length=1000)
    expected_project_updated_at: AwareDatetime
    expected_task_updated_at: AwareDatetime | None = None

    def context(self, access: PermissionAdmission, *, scope: str) -> CommandContext:
        return CommandContext(
            command_id=self.intent_id,
            correlation_id=self.intent_id,
            actor=str(access.principal_id),
            scope=scope,
            reason=self.reason,
            idempotency_key=str(self.intent_id),
        )


class EngineeringPolicyInput(EngineeringIntent):
    expected_generation: int = Field(ge=0)
    independent_reviewer_required: bool

    def command(
        self, *, access: PermissionAdmission, project_id: UUID
    ) -> PublishApprovalPolicy:
        return PublishApprovalPolicy(
            context=self.context(access, scope=SCOPE),
            access=access,
            project_id=project_id,
            expected_project_updated_at=self.expected_project_updated_at,
            expected_generation=self.expected_generation,
            independent_reviewer_required=self.independent_reviewer_required,
        )


class EngineeringDraftInput(EngineeringIntent):
    plan_id: UUID
    expected_generation: int = Field(ge=0)
    content: EngineeringPlanContent

    def command(
        self, *, access: PermissionAdmission, project_id: UUID, task_id: UUID | None
    ) -> DraftEngineeringPlan:
        return DraftEngineeringPlan(
            context=self.context(access, scope=SCOPE),
            access=access,
            plan_id=self.plan_id,
            project_id=project_id,
            task_id=task_id,
            expected_project_updated_at=self.expected_project_updated_at,
            expected_task_updated_at=self.expected_task_updated_at,
            expected_generation=self.expected_generation,
            content=self.content,
        )


class EngineeringDecisionInput(EngineeringIntent):
    plan_id: UUID
    revision_id: UUID
    expected_generation: int = Field(ge=0)
    expected_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_policy_revision_id: UUID
    decision: Literal["approve", "reject", "revoke"]

    def command(
        self, *, access: PermissionAdmission, project_id: UUID, task_id: UUID | None
    ) -> DecideEngineeringPlan:
        return DecideEngineeringPlan(
            context=self.context(access, scope=SCOPE),
            access=access,
            plan_id=self.plan_id,
            project_id=project_id,
            task_id=task_id,
            expected_project_updated_at=self.expected_project_updated_at,
            expected_task_updated_at=self.expected_task_updated_at,
            expected_generation=self.expected_generation,
            revision_id=self.revision_id,
            expected_digest=self.expected_digest,
            expected_policy_revision_id=self.expected_policy_revision_id,
            decision=PlanDecisionKind(self.decision),
        )


class EngineeringReviewInput(EngineeringDecisionInput):
    decision: Literal["approve", "reject"]


class EngineeringRevocationInput(EngineeringDecisionInput):
    decision: Literal["revoke"] = "revoke"


class EngineeringIssuanceInput(EngineeringIntent):
    plan_id: UUID
    revision_id: UUID
    approval_id: UUID
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    infrastructure: InfrastructureReference
    location: PlanLocation
    classification: ExplicitWorkOrderClassification
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=8000)
    priority: WorkOrderCreationPriority

    def command(
        self, *, access: PermissionAdmission, project_id: UUID, task_id: UUID | None
    ) -> IssueProjectWorkOrder:
        return IssueProjectWorkOrder(
            context=self.context(access, scope=PERMISSION),
            access=access,
            project_id=project_id,
            task_id=task_id,
            expected_project_updated_at=self.expected_project_updated_at,
            expected_task_updated_at=self.expected_task_updated_at,
            plan_id=self.plan_id,
            revision_id=self.revision_id,
            approval_id=self.approval_id,
            digest=self.digest,
            infrastructure=self.infrastructure,
            location=self.location,
            classification=self.classification,
            title=self.title,
            description=self.description,
            priority=self.priority,
        )


class EngineeringFormAction(StrEnum):
    policy = "policy"
    draft = "draft"
    review = "review"
    revoke = "revoke"
    issue = "issue"


class EngineeringFormValues(BaseModel):
    """Raw browser strings retained exactly after validation/staleness failures."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    intent_id: str = ""
    expected_project_updated_at: str = ""
    expected_task_updated_at: str = ""
    expected_generation: str = ""
    plan_id: str = ""
    revision_id: str = ""
    approval_id: str = ""
    digest: str = ""
    expected_policy_revision_id: str = ""
    independent_reviewer_required: str = ""
    scope_statement: str = ""
    infrastructure_type: str = ""
    infrastructure_id: str = ""
    location_kind: str = ""
    location_description: str = ""
    latitude: str = ""
    longitude: str = ""
    pop_site_id: str = ""
    criteria: str = ""
    decision: str = ""
    title: str = ""
    description: str = ""
    purpose: str = ""
    priority: str = ""
    reason: str = ""


def browser_input(
    *,
    action: EngineeringFormAction,
    values: EngineeringFormValues,
    confirmed: bool,
    workspace: EngineeringWorkspace | None,
    preview: ProjectDispatchPreview | None,
) -> (
    EngineeringPolicyInput
    | EngineeringDraftInput
    | EngineeringReviewInput
    | EngineeringRevocationInput
    | EngineeringIssuanceInput
):
    """Validate browser text through the same contracts used by the API."""
    common: dict[str, object] = {
        "confirmed": confirmed,
        "intent_id": values.intent_id,
        "reason": values.reason,
        "expected_project_updated_at": values.expected_project_updated_at,
        "expected_task_updated_at": values.expected_task_updated_at or None,
    }
    if action == EngineeringFormAction.policy:
        if values.independent_reviewer_required not in {"true", "false"}:
            raise ValueError(
                "Explicitly choose the project's reviewer independence policy."
            )
        return EngineeringPolicyInput.model_validate(
            {
                **common,
                "expected_generation": values.expected_generation,
                "independent_reviewer_required": values.independent_reviewer_required
                == "true",
            }
        )
    if action == EngineeringFormAction.draft:
        if workspace is None:
            raise ValueError(
                "An exact native Project infrastructure target is required."
            )
        intent = UUID(values.intent_id)
        criteria = []
        for index, line in enumerate(values.criteria.splitlines()):
            statement, separator, verification = line.partition("|")
            if not separator:
                raise ValueError(
                    "Each criterion requires a statement | verification method."
                )
            criteria.append(
                EngineeringCriterion(
                    id=uuid5(intent, str(index)),
                    order=index,
                    statement=statement.strip(),
                    verification_method=verification.strip(),
                )
            )
        location: PlanLocation
        if values.location_kind == "surveyed":
            location = SurveyedPlanLocation(
                description=values.location_description,
                point=GeoPoint(
                    latitude=Decimal(values.latitude),
                    longitude=Decimal(values.longitude),
                ),
            )
        elif values.location_kind == "pop_site":
            location = PopSitePlanLocation(pop_site_id=UUID(values.pop_site_id))
        else:
            raise ValueError("Choose a governed location source.")
        return EngineeringDraftInput.model_validate(
            {
                **common,
                "plan_id": values.plan_id,
                "expected_generation": values.expected_generation,
                "content": EngineeringPlanContent(
                    technology=workspace.technology,
                    infrastructure=InfrastructureReference(
                        type=InfrastructureType(values.infrastructure_type),
                        id=UUID(values.infrastructure_id),
                    ),
                    location=location,
                    scope_statement=values.scope_statement,
                    criteria=tuple(criteria),
                ),
            }
        )
    if action in {EngineeringFormAction.review, EngineeringFormAction.revoke}:
        model = (
            EngineeringReviewInput
            if action == EngineeringFormAction.review
            else EngineeringRevocationInput
        )
        return model.model_validate(
            {
                **common,
                "plan_id": values.plan_id,
                "revision_id": values.revision_id,
                "expected_generation": values.expected_generation,
                "expected_digest": values.digest,
                "expected_policy_revision_id": values.expected_policy_revision_id,
                "decision": values.decision
                if action == EngineeringFormAction.review
                else "revoke",
            }
        )
    if preview is None:
        raise ValueError("Dispatch scope preview is required.")
    if values.location_kind == "surveyed":
        issued_location: PlanLocation = SurveyedPlanLocation(
            description=values.location_description,
            point=GeoPoint(
                latitude=Decimal(values.latitude), longitude=Decimal(values.longitude)
            ),
        )
    elif values.location_kind == "pop_site":
        issued_location = PopSitePlanLocation(pop_site_id=UUID(values.pop_site_id))
    else:
        raise ValueError("Choose a governed location source.")
    return EngineeringIssuanceInput.model_validate(
        {
            **common,
            "plan_id": values.plan_id,
            "revision_id": values.revision_id,
            "approval_id": values.approval_id,
            "digest": values.digest,
            "infrastructure": InfrastructureReference(
                type=InfrastructureType(values.infrastructure_type),
                id=UUID(values.infrastructure_id),
            ),
            "location": issued_location,
            "classification": ExplicitWorkOrderClassification(
                purpose=WorkOrderPurpose(values.purpose),
                access_technology=WorkOrderAccessTechnology(preview.technology.value),
            ),
            "title": values.title,
            "description": values.description or None,
            "priority": values.priority,
        }
    )
