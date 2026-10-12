"""Pure staff form composition over canonical accepted-completion preview."""

from dataclasses import dataclass
from uuid import uuid4

from app.services.action_forms import (
    ActionField,
    ActionFieldKind,
    ActionForm,
    ActionHiddenValue,
)
from app.services.field.completion_project_scope_contracts import (
    AcceptedProjectCompletionPreview,
)
from app.services.form_contracts import FormConsequence, FormContract, register
from app.services.web_action_readiness import ReadinessPanel, readiness_panel

CONTRACT = register(
    FormContract(
        key="project.apply_accepted_field_completion",
        title="Apply accepted field completion",
        entity="project_field_scope",
        command_owner="operations.field_completion",
        consequences=(
            FormConsequence(
                key="complete_scope",
                label="Completes this project or task using all current accepted enrolled field requirements and records immutable evidence.",
            ),
        ),
    )
)


@dataclass(frozen=True, slots=True)
class AcceptedCompletionPage:
    preview: AcceptedProjectCompletionPreview | None
    form: ActionForm | None
    panel: ReadinessPanel | None
    error: str | None
    values: tuple[tuple[str, str], ...]


def compose_accepted_completion_page(
    *,
    preview: AcceptedProjectCompletionPreview | None,
    action_url: str,
    values: dict[str, str] | None = None,
    error: str | None = None,
) -> AcceptedCompletionPage:
    if values is None and preview is not None:
        intent = str(uuid4())
        values = {
            "intent_id": intent,
            "command_id": str(uuid4()),
            "correlation_id": intent,
            "expected_project_revision": preview.project_revision.isoformat(),
            "expected_requirement_token": preview.requirement_token,
            "reason": "",
        }
        if preview.task_revision is not None:
            values["expected_task_revision"] = preview.task_revision.isoformat()
    values = values or {}
    ready = (
        preview is not None
        and preview.readiness.state.value == "ready"
        and error is None
    )
    form = None
    if preview is not None or values:
        form = ActionForm(
            key=CONTRACT.key,
            title=CONTRACT.title,
            description=CONTRACT.consequences[0].label,
            action_url=action_url,
            submit_label="Complete using accepted field evidence",
            fields=(
                ActionField(
                    key="reason",
                    label="Reason for completing this scope",
                    kind=ActionFieldKind.textarea,
                    value=values.get("reason", ""),
                    required=True,
                    max_length=2000,
                    rows=3,
                ),
            ),
            hidden_values=tuple(
                ActionHiddenValue(k, v)
                for k, v in values.items()
                if k != "reason" and v.strip()
            ),
            allowed=ready,
            disabled_reason=None
            if ready
            else error or "Current accepted field evidence is required",
            general_error=error,
        )
    return AcceptedCompletionPage(
        preview,
        form,
        readiness_panel(preview.readiness) if preview else None,
        error,
        tuple(values.items()),
    )
