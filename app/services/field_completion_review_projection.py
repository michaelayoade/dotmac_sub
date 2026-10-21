"""Typed supervisor page presentation over protected completion owner snapshots."""

from dataclasses import dataclass, replace
from enum import StrEnum
from urllib.parse import quote
from uuid import uuid4

from app.schemas.field_completion_review import (
    CompletionCriterionAuthoringValues,
    CompletionCriterionFormValues,
    CompletionFormValues,
)
from app.services.action_forms import (
    ActionConfirmation,
    ActionField,
    ActionFieldKind,
    ActionForm,
    ActionHiddenValue,
    ActionOption,
)
from app.services.field.completion_review_contracts import (
    CompletionPolicyScopeKind,
    CompletionPolicySnapshot,
    CompletionProvenance,
    CompletionReviewOutcome,
    CompletionWorkspace,
    FailedScopeReplacementOutcome,
    FailedScopeReplacementWorkspace,
    ReplacementWorkspacePurpose,
    UnableToCompleteFailure,
)
from app.services.form_contracts import FormConsequence, FormContract, register
from app.services.web_action_readiness import ReadinessPanel, readiness_panel


class CompletionPageAction(StrEnum):
    overview = "overview"
    publish_policy = "publish_policy"
    enroll = "enroll"
    publish_criteria = "publish_criteria"
    decide = "decide"
    revoke = "revoke"


FORM_CONTRACTS = tuple(
    register(
        FormContract(
            key=f"field_completion.{action.value}",
            title={
                CompletionPageAction.publish_policy: "Publish completion review policy",
                CompletionPageAction.enroll: "Enroll work order for completion review",
                CompletionPageAction.publish_criteria: "Publish native completion criteria revision",
                CompletionPageAction.decide: "Record supervisor criterion review",
                CompletionPageAction.revoke: "Withdraw current review decision",
            }[action],
            entity="work_order_completion_enrollment",
            command_owner="operations.field_completion",
            consequences=(
                FormConsequence(
                    key="immutable_review",
                    label="Records immutable policy or review evidence with audit and receipt.",
                ),
                FormConsequence(
                    key="separate_execution",
                    label="Field execution completion remains separate. This action does not close a ticket or project, provision service, or release payment.",
                ),
            ),
        )
    )
    for action in (
        CompletionPageAction.publish_policy,
        CompletionPageAction.enroll,
        CompletionPageAction.publish_criteria,
        CompletionPageAction.decide,
        CompletionPageAction.revoke,
    )
)


@dataclass(frozen=True, slots=True, kw_only=True)
class CompletionPage:
    action: CompletionPageAction
    workspace: CompletionWorkspace | None
    policy: CompletionPolicySnapshot | None
    form: ActionForm | None
    panel: ReadinessPanel | None
    contract: FormContract | None
    title: str
    values: CompletionFormValues | None
    receipt: CompletionReviewOutcome | None = None
    error: str | None = None
    stale: bool = False
    criterion_rows: int | None = None

    @property
    def work_url(self) -> str | None:
        return (
            f"/admin/dispatch/work-orders/{quote(self.workspace.work_order_id, safe='')}/completion-review"
            if self.workspace
            else None
        )


def _selection(
    key: str,
    label: str,
    value: str,
    options: tuple[ActionOption, ...],
    help_text: str | None = None,
) -> ActionField:
    if value and value not in {option.value for option in options}:
        options += (ActionOption(value=value, label=f"Submitted value: {value}"),)
    return ActionField(
        key=key,
        label=label,
        kind=ActionFieldKind.select,
        value=value,
        required=True,
        options=options,
        help_text=help_text,
    )


def completion_page(
    *,
    action: CompletionPageAction,
    workspace: CompletionWorkspace | None = None,
    policy: CompletionPolicySnapshot | None = None,
    submitted: CompletionFormValues | None = None,
    error: str | None = None,
    stale: bool = False,
    receipt: CompletionReviewOutcome | None = None,
    criterion_rows: int | None = None,
) -> CompletionPage:
    if action == CompletionPageAction.overview:
        if workspace is None:
            raise ValueError("Protected workspace required")
        return CompletionPage(
            action=action,
            workspace=workspace,
            policy=workspace.policy,
            form=None,
            panel=None,
            contract=None,
            title="Completion evidence and supervisor review",
            values=None,
            error=error,
            receipt=receipt,
        )
    if action == CompletionPageAction.publish_policy:
        if policy is None:
            raise ValueError("Protected policy snapshot required")
        readiness = policy.publish_readiness
        action_url = (
            f"/admin/completion-review/projects/{policy.scope.project_id}/policy"
            if policy.scope.kind == CompletionPolicyScopeKind.project
            else f"/admin/dispatch/work-orders/{quote(policy.scope.work_order_public_id or '', safe='')}/completion-review/policy"
        )
    else:
        if workspace is None:
            raise ValueError("Protected workspace required")
        readiness = {
            CompletionPageAction.decide: workspace.decide_readiness,
            CompletionPageAction.revoke: workspace.revoke_readiness,
            CompletionPageAction.enroll: workspace.enroll_readiness,
            CompletionPageAction.publish_criteria: workspace.publish_criteria_readiness,
        }[action]
        suffix = {
            CompletionPageAction.decide: "review",
            CompletionPageAction.revoke: "revoke",
            CompletionPageAction.enroll: "enroll",
            CompletionPageAction.publish_criteria: "criteria",
        }[action]
        action_url = f"/admin/dispatch/work-orders/{quote(workspace.work_order_id, safe='')}/completion-review/{suffix}"
    contract = next(
        item
        for item in FORM_CONTRACTS
        if item.key == f"field_completion.{action.value}"
    )
    policy = policy or (workspace.policy if workspace else None)
    chosen_policy = policy or (workspace.policy if workspace else None)
    initial_criteria = workspace.criteria if workspace else ()
    row_count = (
        criterion_rows if criterion_rows is not None else max(1, len(initial_criteria))
    )
    values = submitted or CompletionFormValues(
        intent_id=str(uuid4()),
        enrollment_id=str(workspace.enrollment_id)
        if workspace and workspace.enrollment_id
        else "",
        provenance=workspace.provenance.value
        if workspace and workspace.provenance
        else "",
        engineering_issuance_id=str(workspace.engineering_issuance_id)
        if workspace and workspace.engineering_issuance_id
        else "",
        policy_scope_kind=chosen_policy.scope.kind.value if chosen_policy else "",
        policy_project_id=str(chosen_policy.scope.project_id)
        if chosen_policy and chosen_policy.scope.project_id
        else "",
        policy_work_order_public_id=chosen_policy.scope.work_order_public_id or ""
        if chosen_policy
        else "",
        criteria_generation=str(workspace.criteria_generation) if workspace else "",
        expected_generation=str(
            workspace.criteria_generation
            if action == CompletionPageAction.publish_criteria and workspace
            else policy.generation
            if action == CompletionPageAction.publish_policy and policy
            else workspace.current.generation
            if workspace
            else 0
        ),
        policy_revision_id=str(policy.revision_id)
        if policy and policy.revision_id
        else "",
        submission_id=str(workspace.current.submission_id)
        if workspace and workspace.current.submission_id
        else "",
        decision_id=str(workspace.current.decision_id)
        if workspace and workspace.current.decision_id
        else "",
        independent_reviewer_required="true"
        if policy and policy.independent_reviewer_required is True
        else "false"
        if policy and policy.independent_reviewer_required is False
        else "select",
        criteria=tuple(
            CompletionCriterionAuthoringValues(
                criterion_id=str(initial_criteria[index].id)
                if index < len(initial_criteria)
                else str(uuid4()),
                statement=initial_criteria[index].statement
                if index < len(initial_criteria)
                else "",
                verification_method=initial_criteria[index].verification_method
                if index < len(initial_criteria)
                else "",
            )
            for index in range(row_count)
        )
        if action
        in {CompletionPageAction.enroll, CompletionPageAction.publish_criteria}
        and workspace
        and workspace.provenance == CompletionProvenance.native_work_order
        else (),
        results=tuple(
            CompletionCriterionFormValues(criterion_id=str(item.id), verdict="select")
            for item in workspace.criteria
        )
        if workspace and action == CompletionPageAction.decide
        else (),
    )
    fields: list[ActionField] = []
    hidden = [
        ActionHiddenValue(key=name, value=str(getattr(values, name)))
        for name in (
            "intent_id",
            "enrollment_id",
            "provenance",
            "engineering_issuance_id",
            "policy_scope_kind",
            "policy_project_id",
            "policy_work_order_public_id",
            "criteria_generation",
            "expected_generation",
            "policy_revision_id",
            "submission_id",
            "decision_id",
        )
        if getattr(values, name)
    ]
    if action == CompletionPageAction.publish_policy:
        fields.append(
            _selection(
                "independent_reviewer_required",
                "Require a different reviewing Person",
                values.independent_reviewer_required,
                (
                    ActionOption("select", "Select explicit review policy"),
                    ActionOption("true", "Required"),
                    ActionOption("false", "Not required"),
                ),
            )
        )
    elif action in {CompletionPageAction.enroll, CompletionPageAction.publish_criteria}:
        hidden.append(
            ActionHiddenValue(key="criterion_count", value=str(len(values.criteria)))
        )
        for index, authored in enumerate(values.criteria):
            hidden.append(
                ActionHiddenValue(
                    key=f"criterion_{index}_id",
                    value=authored.criterion_id or "missing",
                )
            )
            fields.extend(
                (
                    ActionField(
                        key=f"criterion_{index}_statement",
                        label=f"Criterion {index + 1} requirement",
                        kind=ActionFieldKind.textarea,
                        value=authored.statement,
                        required=True,
                        rows=3,
                    ),
                    ActionField(
                        key=f"criterion_{index}_verification_method",
                        label=f"Criterion {index + 1} verification method",
                        kind=ActionFieldKind.textarea,
                        value=authored.verification_method,
                        required=True,
                        rows=3,
                    ),
                )
            )
    elif action == CompletionPageAction.decide:
        assert workspace is not None
        criteria = {str(item.id): item for item in workspace.criteria}
        hidden.append(
            ActionHiddenValue(key="result_count", value=str(len(values.results)))
        )
        for index, result in enumerate(values.results):
            criterion = criteria.get(result.criterion_id)
            label = (
                criterion.statement if criterion else f"Submitted criterion {index + 1}"
            )
            hidden.append(
                ActionHiddenValue(
                    key=f"result_{index}_criterion_id",
                    value=result.criterion_id or "missing",
                )
            )
            fields.append(
                _selection(
                    f"result_{index}_verdict",
                    label,
                    result.verdict,
                    (
                        ActionOption("select", "Select explicit verdict"),
                        ActionOption("passed", "Passed"),
                        ActionOption("failed", "Failed"),
                    ),
                    criterion.verification_method if criterion else None,
                )
            )
            fields.append(
                ActionField(
                    key=f"result_{index}_reason",
                    label=f"Criterion {index + 1} review reason",
                    kind=ActionFieldKind.textarea,
                    value=result.reason,
                    required=True,
                    max_length=4000,
                    rows=3,
                )
            )
    fields.append(
        ActionField(
            key="reason",
            label="Reason for this action",
            kind=ActionFieldKind.textarea,
            value=values.reason,
            required=True,
            max_length=1000,
            rows=3,
        )
    )
    form: ActionForm | None = ActionForm(
        key=contract.key,
        title=contract.title,
        description="Review the exact submitted evidence before recording an explicit decision.",
        action_url=action_url,
        submit_label=contract.title,
        fields=tuple(fields),
        hidden_values=tuple(hidden),
        confirmation=ActionConfirmation(
            title="Confirm reviewed evidence",
            message="I reviewed the exact scope, policy and evidence for this action.",
        ),
        impact="Records review evidence. Assignment, execution completion and operational closure remain separate.",
        general_error=error,
    ).gated_by(readiness)
    if stale:
        assert form is not None
        from dataclasses import replace

        form = replace(
            form,
            allowed=False,
            disabled_reason="Original intent and values preserved. Reload current review information before a new intent.",
        )
    if receipt is not None:
        form = None
    return CompletionPage(
        action=action,
        workspace=workspace,
        policy=policy,
        form=form,
        panel=readiness_panel(readiness),
        contract=contract,
        title=contract.title,
        values=values,
        error=error,
        stale=stale,
        receipt=receipt,
        criterion_rows=len(values.criteria) if values.criteria else None,
    )


REPLACEMENT_CONTRACTS = tuple(
    register(
        FormContract(
            key=f"field_completion.{'review' if purpose == ReplacementWorkspacePurpose.review else 'revoke'}_failed_scope_replacement",
            title="Review failed-scope replacement"
            if purpose == ReplacementWorkspacePurpose.review
            else "Revoke replacement coverage",
            entity="work_order_completion_enrollment",
            command_owner="operations.field_completion",
            consequences=(
                FormConsequence(
                    key="coverage_only",
                    label=(
                        "Approves criterion coverage only. Replacement completion still requires supervisor acceptance under its versioned independence policy before customer confirmation."
                        if purpose == ReplacementWorkspacePurpose.review
                        else "Withdraws current replacement coverage. The original failed scope remains blocking without a valid reviewed replacement and accepted completion."
                    ),
                ),
            ),
        )
    )
    for purpose in (
        ReplacementWorkspacePurpose.review,
        ReplacementWorkspacePurpose.revoke,
    )
)


@dataclass(frozen=True, slots=True, kw_only=True)
class ReplacementFormValues:
    """Raw browser intent retained exactly across validation and stale refusals."""

    fields: tuple[tuple[str, str], ...]

    def get(self, key: str, default: str = "") -> str:
        return next((value for name, value in self.fields if name == key), default)


@dataclass(frozen=True, slots=True, kw_only=True)
class ReplacementPage:
    workspace: FailedScopeReplacementWorkspace | None
    purpose: ReplacementWorkspacePurpose
    title: str
    form: ActionForm | None
    panel: ReadinessPanel | None
    values: ReplacementFormValues
    contract: FormContract
    error: str | None = None
    stale: bool = False
    receipt: FailedScopeReplacementOutcome | None = None


def replacement_page(
    *,
    workspace: FailedScopeReplacementWorkspace,
    purpose: ReplacementWorkspacePurpose,
    candidate_id: str = "",
    submitted: ReplacementFormValues | None = None,
    error: str | None = None,
    stale: bool = False,
    receipt: FailedScopeReplacementOutcome | None = None,
) -> ReplacementPage:
    source = workspace.source
    candidate = next(
        (
            item
            for item in workspace.candidates
            if str(item.enrollment_id) == candidate_id
        ),
        None,
    )
    contract = next(
        item
        for item in REPLACEMENT_CONTRACTS
        if item.key == f"field_completion.{purpose.value}_failed_scope_replacement"
    )
    readiness = (
        workspace.review_readiness
        if purpose == ReplacementWorkspacePurpose.review
        else workspace.revoke_readiness
    )
    initial = {
        "intent_id": str(uuid4()),
        "source_enrollment_id": str(source.enrollment_id),
        "expected_generation": str(workspace.expected_generation),
        "reason": "",
    }
    if purpose == ReplacementWorkspacePurpose.revoke:
        initial["decision_id"] = (
            str(workspace.current_decision.decision_id)
            if workspace.current_decision
            else ""
        )
    else:
        for prefix, snapshot in (("source", source), ("replacement", candidate)):
            if snapshot:
                for attr in (
                    "criteria_revision_id",
                    "policy_revision_id",
                    "assignment_generation",
                ):
                    initial[f"{prefix}_{attr}"] = str(getattr(snapshot, attr) or "")
        initial["replacement_enrollment_id"] = candidate_id
        failure = source.failure_basis
        if isinstance(failure, UnableToCompleteFailure):
            initial.update(
                failure_kind="unable_to_complete",
                failure_event_id=str(failure.event_id),
                failure_assignment_generation=str(failure.assignment_generation),
            )
        elif failure:
            initial.update(
                failure_kind="rejected_submission",
                failure_submission_id=str(failure.submission_id),
                failure_decision_id=str(failure.decision_id),
                failure_review_generation=str(failure.review_generation),
            )
        initial["coverage_count"] = str(len(source.criteria))
        for index, criterion in enumerate(source.criteria):
            initial[f"coverage_{index}_source"] = str(criterion.id)
            initial[f"coverage_{index}_replacement"] = "select"
    values = submitted or ReplacementFormValues(fields=tuple(initial.items()))
    fields = []
    if purpose == ReplacementWorkspacePurpose.review:
        count = values.get("coverage_count", "0")
        row_count = min(100, int(count)) if count.isdecimal() and len(count) <= 3 else 0
        for index in range(row_count):
            criterion_id = values.get(f"coverage_{index}_source")
            matched_criterion = next(
                (c for c in source.criteria if str(c.id) == criterion_id), None
            )
            fields.append(
                _selection(
                    f"coverage_{index}_replacement",
                    matched_criterion.statement
                    if matched_criterion
                    else f"Submitted criterion {index + 1}",
                    values.get(f"coverage_{index}_replacement"),
                    (ActionOption("select", "Select replacement criterion"),)
                    + tuple(
                        ActionOption(
                            str(c.id),
                            f"{c.statement} · Verify: {c.verification_method}",
                        )
                        for c in candidate.criteria
                    )
                    if candidate
                    else (ActionOption("select", "Choose a replacement job first"),),
                    matched_criterion.verification_method
                    if matched_criterion
                    else None,
                )
            )
    fields.append(
        ActionField(
            key="reason",
            label="Reason for this reviewed coverage decision",
            kind=ActionFieldKind.textarea,
            required=True,
            value=values.get("reason"),
            max_length=1000,
            rows=3,
        )
    )
    visible = {field.key for field in fields}
    url = f"/admin/dispatch/work-orders/{quote(source.work_order_public_id, safe='')}/completion-review/replacement"
    if purpose == ReplacementWorkspacePurpose.revoke:
        url += "/revoke"
    form = ActionForm(
        key=contract.key,
        title=contract.title,
        description="Review the original failure and exact immutable criteria before deciding coverage.",
        action_url=url,
        submit_label=contract.title,
        fields=tuple(fields),
        hidden_values=tuple(
            ActionHiddenValue(key=key, value=value)
            for key, value in values.fields
            if key not in visible and value
        ),
        confirmation=ActionConfirmation(
            title="Confirm reviewed scope",
            message="I reviewed the exact failure, criterion coverage and versioned reviewer independence policies.",
        ),
        impact=contract.consequences[0].label,
        general_error=error,
    ).gated_by(readiness)
    if stale or (purpose == ReplacementWorkspacePurpose.review and candidate is None):
        form = replace(
            form,
            allowed=False,
            disabled_reason="Reload current information to begin a new intent."
            if stale
            else "Choose a scoped replacement job first.",
        )
    return ReplacementPage(
        workspace=workspace,
        purpose=purpose,
        title=contract.title,
        form=None if receipt else form,
        panel=readiness_panel(readiness),
        values=values,
        contract=contract,
        error=error,
        stale=stale,
        receipt=receipt,
    )


def replacement_unavailable_page(
    *,
    purpose: ReplacementWorkspacePurpose,
    submitted: ReplacementFormValues,
    error: str,
) -> ReplacementPage:
    """Preserve caller-authored draft only; no protected snapshot or receipt fallback."""
    contract = next(
        item
        for item in REPLACEMENT_CONTRACTS
        if item.key == f"field_completion.{purpose.value}_failed_scope_replacement"
    )
    form = ActionForm(
        key=contract.key,
        title=contract.title,
        description="Submitted draft retained while current protected review information is unavailable.",
        action_url="#",
        submit_label=contract.title,
        fields=(
            ActionField(
                key="reason",
                label="Submitted reason",
                kind=ActionFieldKind.textarea,
                value=submitted.get("reason"),
                rows=3,
            ),
        ),
        hidden_values=tuple(
            ActionHiddenValue(key=key, value=value)
            for key, value in submitted.fields
            if key != "reason" and value
        ),
        allowed=False,
        disabled_reason="Reload current information before starting a new intent.",
        general_error=error,
    )
    return ReplacementPage(
        workspace=None,
        purpose=purpose,
        title=contract.title,
        form=form,
        panel=None,
        values=submitted,
        contract=contract,
        error=error,
        stale=True,
    )
