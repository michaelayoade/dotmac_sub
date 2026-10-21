"""Read-only staff page composition; domain owners supply all action readiness."""

from dataclasses import dataclass
from urllib.parse import urlencode
from uuid import UUID, uuid4

from app.schemas.project_engineering import EngineeringFormAction, EngineeringFormValues
from app.services.action_forms import (
    ActionConfirmation,
    ActionField,
    ActionFieldKind,
    ActionForm,
    ActionHiddenValue,
    ActionOption,
)
from app.services.action_readiness import ActionReadiness
from app.services.form_contracts import FormConsequence, FormContract, register
from app.services.project_engineering_plan_contracts import (
    EngineeringWorkspace,
    ProjectDispatchPreview,
    SurveyedPlanLocation,
)
from app.services.project_work_order_handoff_contracts import ProjectWorkOrderIssued
from app.services.web_action_readiness import ReadinessPanel, readiness_panel

FORM_CONTRACTS = tuple(
    register(
        FormContract(
            key=f"project_engineering.{action.value}",
            title={
                EngineeringFormAction.policy: "Publish project approval policy",
                EngineeringFormAction.draft: "Draft engineering revision",
                EngineeringFormAction.review: "Review engineering revision",
                EngineeringFormAction.revoke: "Revoke current approval",
                EngineeringFormAction.issue: "Issue infrastructure work order",
            }[action],
            entity="project_engineering_plan",
            command_owner="operations.project_work_order_handoff"
            if action == EngineeringFormAction.issue
            else "operations.project_engineering_plans",
            consequences=(
                FormConsequence(
                    key="immutable_evidence",
                    label="Records immutable command evidence and an audit event.",
                ),
                FormConsequence(
                    key="separate_work",
                    label="Issuance creates a draft infrastructure WorkOrder. Assignment is a separate action."
                    if action == EngineeringFormAction.issue
                    else "Engineering decisions do not issue or assign field work.",
                ),
            ),
        )
    )
    for action in EngineeringFormAction
)


@dataclass(frozen=True, slots=True, kw_only=True)
class ProjectEngineeringPage:
    workspace: EngineeringWorkspace | None
    preview: ProjectDispatchPreview | None
    title: str
    base_url: str
    form: ActionForm | None
    readiness: ActionReadiness | None
    form_contract: FormContract | None
    issued: ProjectWorkOrderIssued | None = None
    stale: bool = False
    error: str | None = None

    @property
    def panel(self) -> ReadinessPanel | None:
        return readiness_panel(self.readiness) if self.readiness else None


def base_url(project_id: UUID, task_id: UUID | None, technology: str) -> str:
    suffix = urlencode(
        {"technology": technology, **({"task_id": str(task_id)} if task_id else {})}
    )
    return f"/admin/projects/{project_id}/engineering?{suffix}"


def project_page(
    *,
    workspace: EngineeringWorkspace | None = None,
    preview: ProjectDispatchPreview | None = None,
    submitted: EngineeringFormValues | None = None,
    error: str | None = None,
    stale: bool = False,
) -> ProjectEngineeringPage:
    if workspace is None and preview is None:
        raise ValueError("An owner-produced snapshot is required")
    source = workspace if workspace else preview
    assert source is not None
    base = base_url(source.project_id, source.task_id, source.technology.value)
    action = (
        EngineeringFormAction.issue
        if preview
        else EngineeringFormAction(workspace.purpose.value)
        if workspace and workspace.purpose.value != "overview"
        else None
    )
    if action is None:
        return ProjectEngineeringPage(
            workspace=workspace,
            preview=None,
            title="Engineering plans",
            base_url=base,
            form=None,
            readiness=None,
            form_contract=None,
            error=error,
            stale=stale,
        )
    contract = next(
        item
        for item in FORM_CONTRACTS
        if item.key == f"project_engineering.{action.value}"
    )
    approval = preview.approval if preview else None
    revision = workspace.revision if workspace else None
    content = approval.content if approval else revision.content if revision else None
    location = content.location if content else None
    target = (
        workspace.infrastructure
        if workspace and action == EngineeringFormAction.draft
        else content.infrastructure
        if content
        else None
    )
    values = submitted or EngineeringFormValues(
        intent_id=str(uuid4()),
        expected_project_updated_at=source.project_updated_at.isoformat(),
        expected_task_updated_at=source.task_updated_at.isoformat()
        if source.task_updated_at
        else "",
        expected_generation=str(
            workspace.policy_generation
            if action == EngineeringFormAction.policy and workspace is not None
            else workspace.generation
            if workspace
            else 0
        ),
        plan_id=str(approval.plan_id)
        if approval
        else str(workspace.plan_id or uuid4())
        if workspace
        else "",
        revision_id=str(approval.revision_id)
        if approval
        else str(revision.id)
        if revision
        else "",
        approval_id=str(approval.approval_id)
        if approval
        else str(workspace.approval_id)
        if workspace and workspace.approval_id
        else "",
        digest=approval.digest if approval else revision.digest if revision else "",
        expected_policy_revision_id=str(workspace.policy.id)
        if workspace and workspace.policy
        else "",
        independent_reviewer_required="true"
        if workspace
        and workspace.policy
        and workspace.policy.independent_reviewer_required
        else "false"
        if workspace and workspace.policy
        else "select",
        scope_statement=content.scope_statement if content else "",
        infrastructure_type=target.type.value if target else "",
        infrastructure_id=str(target.id) if target else "",
        location_kind=location.kind if location else "surveyed",
        location_description=location.description
        if isinstance(location, SurveyedPlanLocation)
        else "",
        latitude=str(location.point.latitude)
        if isinstance(location, SurveyedPlanLocation)
        else "",
        longitude=str(location.point.longitude)
        if isinstance(location, SurveyedPlanLocation)
        else "",
        pop_site_id=str(location.pop_site_id)
        if location and location.kind == "pop_site"
        else "",
        criteria="\n".join(
            f"{item.statement} | {item.verification_method}"
            for item in content.criteria
        )
        if content
        else "",
        decision="revoke" if action == EngineeringFormAction.revoke else "approve",
        title="Infrastructure work",
        purpose="infrastructure_rollout",
        priority="normal",
    )

    def text(
        key: str,
        label: str,
        *,
        textarea: bool = False,
        required: bool = True,
        help_text: str | None = None,
    ) -> ActionField:
        return ActionField(
            key=key,
            label=label,
            value=str(getattr(values, key)),
            kind=ActionFieldKind.textarea if textarea else ActionFieldKind.text,
            required=required,
            help_text=help_text,
            rows=4 if textarea else None,
        )

    def selection(
        key: str, label: str, options: tuple[tuple[str, str], ...]
    ) -> ActionField:
        return ActionField(
            key=key,
            label=label,
            value=str(getattr(values, key)),
            kind=ActionFieldKind.select,
            required=True,
            options=tuple(
                ActionOption(value=value, label=label) for value, label in options
            ),
        )

    fields: tuple[ActionField, ...] = ()
    if action == EngineeringFormAction.policy:
        fields = (
            selection(
                "independent_reviewer_required",
                "Require an independent reviewing Person",
                (
                    ("select", "Select policy"),
                    ("true", "Required"),
                    ("false", "Not required"),
                ),
            ),
        )
    elif action == EngineeringFormAction.draft:
        fields = (
            text("scope_statement", "Engineering scope", textarea=True),
            selection(
                "location_kind",
                "Location source",
                (("surveyed", "Reviewed survey"), ("pop_site", "Native POP site")),
            ),
            text("location_description", "Survey location", required=False),
            text("latitude", "Latitude (WGS84)", required=False),
            text("longitude", "Longitude (WGS84)", required=False),
            text("pop_site_id", "POP site identifier", required=False),
            text(
                "criteria",
                "Acceptance criteria and verification",
                textarea=True,
                help_text="One criterion per line: acceptance statement | verification method.",
            ),
        )
    elif action == EngineeringFormAction.review:
        fields = (
            selection(
                "decision",
                "Review decision",
                (("approve", "Approve revision"), ("reject", "Reject revision")),
            ),
        )
    elif action == EngineeringFormAction.issue:
        fields = (
            text("title", "Work-order title"),
            text("description", "Field instructions", textarea=True, required=False),
            selection(
                "purpose",
                "Work purpose",
                (
                    ("infrastructure_rollout", "Infrastructure rollout"),
                    ("upgrade", "Upgrade"),
                    ("maintenance", "Maintenance"),
                    ("repair", "Repair"),
                    ("relocation", "Relocation"),
                ),
            ),
            selection(
                "priority",
                "Priority",
                (
                    ("low", "Low"),
                    ("normal", "Normal"),
                    ("high", "High"),
                    ("urgent", "Urgent"),
                ),
            ),
        )
    fields += (text("reason", "Business reason", textarea=True),)
    hidden_names: tuple[str, ...] = (
        "intent_id",
        "expected_project_updated_at",
        "expected_task_updated_at",
        "expected_generation",
        "plan_id",
        "revision_id",
        "approval_id",
        "digest",
        "expected_policy_revision_id",
    )
    if action in {EngineeringFormAction.draft, EngineeringFormAction.issue}:
        hidden_names += ("infrastructure_type", "infrastructure_id")
    if action == EngineeringFormAction.issue:
        hidden_names += (
            "location_kind",
            "location_description",
            "latitude",
            "longitude",
            "pop_site_id",
        )
    hidden = tuple(
        ActionHiddenValue(key=name, value=str(getattr(values, name)))
        for name in hidden_names
        if getattr(values, name)
    )
    action_path = "issuance" if action == EngineeringFormAction.issue else action.value
    path, query_string = base.split("?", 1)
    form = ActionForm(
        key=f"project_engineering.{action.value}",
        title=contract.title,
        description="Review the current evidence and explain this explicit action.",
        action_url=f"{path}/{action_path}?{query_string}",
        submit_label=contract.title,
        fields=fields,
        hidden_values=hidden,
        confirmation=ActionConfirmation(
            title="Confirm this action",
            message="I reviewed the exact project scope and engineering evidence.",
        ),
        impact="No technician assignment, customer notification or service provisioning is performed.",
        general_error=error,
    ).gated_by(source.readiness)
    return ProjectEngineeringPage(
        workspace=workspace,
        preview=preview,
        title=contract.title,
        base_url=base,
        form=form,
        readiness=source.readiness,
        form_contract=contract,
        stale=stale,
        error=error,
    )
