"""Supervisor completion-review pages; business decisions stay in the owner."""

import secrets
from dataclasses import replace
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.csrf import CSRF_COOKIE_NAME, get_csrf_token, set_csrf_cookie
from app.db import get_db
from app.schemas.field_completion_review import (
    CompletionCriteriaInput,
    CompletionCriterionAuthoringValues,
    CompletionCriterionFormValues,
    CompletionDecisionInput,
    CompletionEnrollmentInput,
    CompletionFormValues,
    CompletionPolicyInput,
    CompletionRevocationInput,
)
from app.schemas.field_failed_scope_replacement import (
    ReplacementReviewInput,
    ReplacementRevocationInput,
)
from app.services.auth_dependencies import require_permission_admission
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.field import completion_review as owner
from app.services.field.completion_review_contracts import (
    READ,
    WRITE,
    CompletionPolicyPurpose,
    CompletionPolicyQuery,
    CompletionPolicyScope,
    CompletionPolicyScopeKind,
    CompletionReviewOutcome,
    CompletionWorkspacePurpose,
    CompletionWorkspaceQuery,
    FailedScopeReplacementWorkspaceQuery,
    ReplacementWorkspacePurpose,
)
from app.services.field_completion_review_projection import (
    CompletionPage,
    CompletionPageAction,
    ReplacementFormValues,
    ReplacementPage,
    completion_page,
    replacement_page,
    replacement_unavailable_page,
)
from app.services.permission_admission import PermissionAdmission
from app.web.request_parsing import parse_form_data_sync

router = APIRouter(tags=["web-admin-field-completion-review"])
templates = Jinja2Templates(directory="templates")


def _status(error: DomainError) -> int:
    if error.code.endswith(("not_found",)):
        return 404
    if error.code.endswith(("denied", "forbidden")):
        return 403
    if error.code.endswith("invalid_request"):
        return 422
    return 409


def _policy_scope(
    project_id: UUID | None, work_order_public_id: str | None
) -> CompletionPolicyScope:
    return (
        CompletionPolicyScope(
            kind=CompletionPolicyScopeKind.project, project_id=project_id
        )
        if project_id is not None
        else CompletionPolicyScope(
            kind=CompletionPolicyScopeKind.work_order,
            work_order_public_id=work_order_public_id,
        )
    )


def _page(
    db: Session,
    *,
    access: PermissionAdmission,
    action: CompletionPageAction,
    work_order_public_id: str | None = None,
    project_id: UUID | None = None,
    submitted: CompletionFormValues | None = None,
    error: str | None = None,
    stale: bool = False,
    receipt: CompletionReviewOutcome | None = None,
    criterion_rows: int | None = None,
) -> CompletionPage:
    try:
        if action == CompletionPageAction.publish_policy:
            if project_id is None and work_order_public_id is None:
                raise HTTPException(
                    status_code=422, detail="Explicit policy scope required"
                )
            policy = owner.policy_snapshot(
                db,
                query=CompletionPolicyQuery(
                    access=access,
                    scope=_policy_scope(project_id, work_order_public_id),
                    purpose=CompletionPolicyPurpose.publish_policy,
                ),
            )
            return completion_page(
                action=action,
                policy=policy,
                submitted=submitted,
                error=error,
                stale=stale,
                receipt=receipt,
            )
        if work_order_public_id is None:
            raise HTTPException(status_code=422, detail="Work order scope required")
        workspace = owner.workspace(
            db,
            query=CompletionWorkspaceQuery(
                access=access,
                work_order_public_id=work_order_public_id,
                purpose={
                    CompletionPageAction.overview: CompletionWorkspacePurpose.read,
                    CompletionPageAction.decide: CompletionWorkspacePurpose.decide,
                    CompletionPageAction.revoke: CompletionWorkspacePurpose.revoke,
                    CompletionPageAction.enroll: CompletionWorkspacePurpose.enroll,
                    CompletionPageAction.publish_criteria: CompletionWorkspacePurpose.publish_criteria,
                }[action],
            ),
        )
        return completion_page(
            action=action,
            workspace=workspace,
            submitted=submitted,
            error=error,
            stale=stale,
            receipt=receipt,
            criterion_rows=criterion_rows,
        )
    except DomainError as exc:
        raise HTTPException(status_code=_status(exc), detail=exc.message) from exc


def _render(
    request: Request, db: Session, *, page: CompletionPage, status_code: int = 200
) -> HTMLResponse:
    from app.web.admin import get_current_user, get_sidebar_stats

    token = get_csrf_token(request)
    response = templates.TemplateResponse(
        request=request,
        name="admin/dispatch/completion_review.html",
        context={
            "request": request,
            "page": page,
            "csrf_token": token,
            "active_page": "dispatch-work-orders",
            "active_menu": "operations",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
        status_code=status_code,
    )
    set_csrf_cookie(response, token, request)
    return response


def _text(form: FormData, name: str) -> str:
    value = form.get(name)
    return value if isinstance(value, str) else ""


def _values(form: FormData) -> CompletionFormValues:
    count = _text(form, "result_count") or "0"
    if not count.isdecimal() or not 0 <= int(count) <= 100:
        raise HTTPException(status_code=422, detail="Invalid criterion form count")
    criteria_count = _text(form, "criterion_count") or "0"
    if not criteria_count.isdecimal() or not 0 <= int(criteria_count) <= 100:
        raise HTTPException(status_code=422, detail="Invalid criterion form count")
    return CompletionFormValues(
        intent_id=_text(form, "intent_id"),
        enrollment_id=_text(form, "enrollment_id"),
        criteria_generation=_text(form, "criteria_generation"),
        provenance=_text(form, "provenance"),
        engineering_issuance_id=_text(form, "engineering_issuance_id"),
        policy_scope_kind=_text(form, "policy_scope_kind"),
        policy_project_id=_text(form, "policy_project_id"),
        policy_work_order_public_id=_text(form, "policy_work_order_public_id"),
        criteria=tuple(
            CompletionCriterionAuthoringValues(
                criterion_id=_text(form, f"criterion_{index}_id"),
                statement=_text(form, f"criterion_{index}_statement"),
                verification_method=_text(
                    form, f"criterion_{index}_verification_method"
                ),
            )
            for index in range(int(criteria_count))
        ),
        expected_generation=_text(form, "expected_generation"),
        policy_revision_id=_text(form, "policy_revision_id"),
        submission_id=_text(form, "submission_id"),
        decision_id=_text(form, "decision_id"),
        independent_reviewer_required=_text(form, "independent_reviewer_required"),
        reason=_text(form, "reason"),
        results=tuple(
            CompletionCriterionFormValues(
                criterion_id=_text(form, f"result_{index}_criterion_id"),
                verdict=_text(form, f"result_{index}_verdict"),
                reason=_text(form, f"result_{index}_reason"),
            )
            for index in range(int(count))
        ),
    )


def _submit(
    request: Request,
    db: Session,
    *,
    access: PermissionAdmission,
    action: CompletionPageAction,
    project_id: UUID | None = None,
    work_order_public_id: str | None = None,
) -> HTMLResponse:
    form = parse_form_data_sync(request)
    token = form.get("_csrf_token")
    cookie = request.cookies.get(CSRF_COOKIE_NAME)
    if (
        not isinstance(token, str)
        or not cookie
        or not secrets.compare_digest(token, cookie)
    ):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
    values = _values(form)
    common: dict[str, object] = {
        "intent_id": values.intent_id,
        "reason": values.reason,
        "confirmed": form.get("confirmed") == "yes",
        "expected_generation": values.expected_generation,
    }
    try:
        if action == CompletionPageAction.publish_policy:
            if project_id is None and work_order_public_id is None:
                raise ValueError("Explicit policy scope required")
            if values.independent_reviewer_required not in {"true", "false"}:
                raise ValueError("Choose an explicit reviewer independence policy.")
            policy_input = CompletionPolicyInput.model_validate(
                {
                    **common,
                    "independent_reviewer_required": values.independent_reviewer_required
                    == "true",
                }
            )
            command = policy_input.command(
                access=access, scope=_policy_scope(project_id, work_order_public_id)
            )
            db_session_adapter.release_read_transaction(db)
            result = owner.publish_policy(db, command=command)
        elif action in {
            CompletionPageAction.enroll,
            CompletionPageAction.publish_criteria,
        }:
            criteria = [
                {
                    "id": item.criterion_id,
                    "order": index,
                    "statement": item.statement,
                    "verification_method": item.verification_method,
                }
                for index, item in enumerate(values.criteria)
            ]
            if action == CompletionPageAction.enroll:
                if work_order_public_id is None:
                    raise ValueError("Work order scope required")
                enrollment_input = CompletionEnrollmentInput.model_validate(
                    {
                        "intent_id": values.intent_id,
                        "reason": values.reason,
                        "confirmed": form.get("confirmed") == "yes",
                        "provenance": values.provenance,
                        "policy_scope": {
                            "kind": values.policy_scope_kind,
                            "project_id": values.policy_project_id or None,
                            "work_order_public_id": values.policy_work_order_public_id
                            or None,
                        },
                        "engineering_issuance_id": values.engineering_issuance_id
                        or None,
                        "criteria": criteria,
                    }
                )
                enrollment = enrollment_input.command(
                    access=access, work_order_public_id=work_order_public_id
                )
                db_session_adapter.release_read_transaction(db)
                result = owner.enroll(db, command=enrollment)
            else:
                criteria_input = CompletionCriteriaInput.model_validate(
                    {**common, "criteria": criteria}
                )
                criteria_command = criteria_input.command(
                    access=access,
                    enrollment_id=UUID(values.enrollment_id),
                    work_order_public_id=work_order_public_id,
                )
                db_session_adapter.release_read_transaction(db)
                result = owner.publish_criteria(db, command=criteria_command)
        elif action == CompletionPageAction.decide:
            decision_input = CompletionDecisionInput.model_validate(
                {
                    **common,
                    "submission_id": values.submission_id,
                    "policy_revision_id": values.policy_revision_id,
                    "results": [
                        {
                            "criterion_id": item.criterion_id,
                            "verdict": item.verdict,
                            "reason": item.reason,
                        }
                        for item in values.results
                    ],
                }
            )
            decision = decision_input.command(
                access=access,
                enrollment_id=UUID(values.enrollment_id),
                work_order_public_id=work_order_public_id,
            )
            db_session_adapter.release_read_transaction(db)
            result = owner.decide(db, command=decision)
        else:
            revocation_input = CompletionRevocationInput.model_validate(
                {**common, "decision_id": values.decision_id}
            )
            revocation = revocation_input.command(
                access=access,
                enrollment_id=UUID(values.enrollment_id),
                work_order_public_id=work_order_public_id,
            )
            db_session_adapter.release_read_transaction(db)
            result = owner.revoke(db, command=revocation)
    except (ValidationError, ValueError) as exc:
        return _render(
            request,
            db,
            page=_page(
                db,
                access=access,
                action=action,
                project_id=project_id,
                work_order_public_id=work_order_public_id,
                submitted=values,
                error="Check the entered values and required confirmation. "
                + str(exc).splitlines()[0],
            ),
            status_code=422,
        )
    except DomainError as exc:
        status = _status(exc)
        if status in {403, 404}:
            raise HTTPException(status_code=status, detail=exc.message) from exc
        return _render(
            request,
            db,
            page=_page(
                db,
                access=access,
                action=action,
                project_id=project_id,
                work_order_public_id=work_order_public_id,
                submitted=values,
                error=exc.message,
                stale=status == 409,
            ),
            status_code=status,
        )
    page = _page(
        db,
        access=access,
        action=action,
        project_id=project_id,
        work_order_public_id=work_order_public_id,
        submitted=values,
        receipt=result,
    )
    return _render(request, db, page=replace(page, form=None))


@router.get(
    "/completion-review/projects/{project_id}/policy", response_class=HTMLResponse
)
def policy(
    request: Request,
    project_id: UUID,
    access: PermissionAdmission = Depends(require_permission_admission(WRITE)),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _render(
        request,
        db,
        page=_page(
            db,
            access=access,
            action=CompletionPageAction.publish_policy,
            project_id=project_id,
        ),
    )


@router.post(
    "/completion-review/projects/{project_id}/policy", response_class=HTMLResponse
)
def publish_policy(
    request: Request,
    project_id: UUID,
    access: PermissionAdmission = Depends(require_permission_admission(WRITE)),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _submit(
        request,
        db,
        access=access,
        action=CompletionPageAction.publish_policy,
        project_id=project_id,
    )


@router.get(
    "/dispatch/work-orders/{work_order_public_id}/completion-review",
    response_class=HTMLResponse,
)
def overview(
    request: Request,
    work_order_public_id: str,
    access: PermissionAdmission = Depends(require_permission_admission(READ)),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _render(
        request,
        db,
        page=_page(
            db,
            access=access,
            action=CompletionPageAction.overview,
            work_order_public_id=work_order_public_id,
        ),
    )


@router.get(
    "/dispatch/work-orders/{work_order_public_id}/completion-review/review",
    response_class=HTMLResponse,
)
def review(
    request: Request,
    work_order_public_id: str,
    access: PermissionAdmission = Depends(require_permission_admission(WRITE)),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _render(
        request,
        db,
        page=_page(
            db,
            access=access,
            action=CompletionPageAction.decide,
            work_order_public_id=work_order_public_id,
        ),
    )


@router.post(
    "/dispatch/work-orders/{work_order_public_id}/completion-review/review",
    response_class=HTMLResponse,
)
def decide(
    request: Request,
    work_order_public_id: str,
    access: PermissionAdmission = Depends(require_permission_admission(WRITE)),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _submit(
        request,
        db,
        access=access,
        action=CompletionPageAction.decide,
        work_order_public_id=work_order_public_id,
    )


@router.get(
    "/dispatch/work-orders/{work_order_public_id}/completion-review/revoke",
    response_class=HTMLResponse,
)
def withdrawal(
    request: Request,
    work_order_public_id: str,
    access: PermissionAdmission = Depends(require_permission_admission(WRITE)),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _render(
        request,
        db,
        page=_page(
            db,
            access=access,
            action=CompletionPageAction.revoke,
            work_order_public_id=work_order_public_id,
        ),
    )


@router.post(
    "/dispatch/work-orders/{work_order_public_id}/completion-review/revoke",
    response_class=HTMLResponse,
)
def revoke(
    request: Request,
    work_order_public_id: str,
    access: PermissionAdmission = Depends(require_permission_admission(WRITE)),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _submit(
        request,
        db,
        access=access,
        action=CompletionPageAction.revoke,
        work_order_public_id=work_order_public_id,
    )


@router.get(
    "/dispatch/work-orders/{work_order_public_id}/completion-review/policy",
    response_class=HTMLResponse,
)
def work_order_policy(
    request: Request,
    work_order_public_id: str,
    access: PermissionAdmission = Depends(require_permission_admission(WRITE)),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _render(
        request,
        db,
        page=_page(
            db,
            access=access,
            action=CompletionPageAction.publish_policy,
            work_order_public_id=work_order_public_id,
        ),
    )


@router.post(
    "/dispatch/work-orders/{work_order_public_id}/completion-review/policy",
    response_class=HTMLResponse,
)
def publish_work_order_policy(
    request: Request,
    work_order_public_id: str,
    access: PermissionAdmission = Depends(require_permission_admission(WRITE)),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _submit(
        request,
        db,
        access=access,
        action=CompletionPageAction.publish_policy,
        work_order_public_id=work_order_public_id,
    )


@router.get(
    "/dispatch/work-orders/{work_order_public_id}/completion-review/enroll",
    response_class=HTMLResponse,
)
def enrollment(
    request: Request,
    work_order_public_id: str,
    criterion_rows: int | None = Query(None, ge=1, le=100),
    access: PermissionAdmission = Depends(require_permission_admission(WRITE)),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _render(
        request,
        db,
        page=_page(
            db,
            access=access,
            action=CompletionPageAction.enroll,
            work_order_public_id=work_order_public_id,
            criterion_rows=criterion_rows,
        ),
    )


@router.post(
    "/dispatch/work-orders/{work_order_public_id}/completion-review/enroll",
    response_class=HTMLResponse,
)
def enroll(
    request: Request,
    work_order_public_id: str,
    access: PermissionAdmission = Depends(require_permission_admission(WRITE)),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _submit(
        request,
        db,
        access=access,
        action=CompletionPageAction.enroll,
        work_order_public_id=work_order_public_id,
    )


@router.get(
    "/dispatch/work-orders/{work_order_public_id}/completion-review/criteria",
    response_class=HTMLResponse,
)
def criteria(
    request: Request,
    work_order_public_id: str,
    criterion_rows: int | None = Query(None, ge=1, le=100),
    access: PermissionAdmission = Depends(require_permission_admission(WRITE)),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _render(
        request,
        db,
        page=_page(
            db,
            access=access,
            action=CompletionPageAction.publish_criteria,
            work_order_public_id=work_order_public_id,
            criterion_rows=criterion_rows,
        ),
    )


@router.post(
    "/dispatch/work-orders/{work_order_public_id}/completion-review/criteria",
    response_class=HTMLResponse,
)
def publish_criteria(
    request: Request,
    work_order_public_id: str,
    access: PermissionAdmission = Depends(require_permission_admission(WRITE)),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _submit(
        request,
        db,
        access=access,
        action=CompletionPageAction.publish_criteria,
        work_order_public_id=work_order_public_id,
    )


def _replacement_render(
    request: Request, db: Session, *, page: ReplacementPage, status_code: int = 200
) -> HTMLResponse:
    from app.web.admin import get_current_user, get_sidebar_stats

    token = get_csrf_token(request)
    response = templates.TemplateResponse(
        request=request,
        name="admin/dispatch/replacement_review.html",
        context={
            "request": request,
            "page": page,
            "csrf_token": token,
            "active_page": "dispatch-work-orders",
            "active_menu": "operations",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
        status_code=status_code,
    )
    set_csrf_cookie(response, token, request)
    return response


def _replacement_get(
    request: Request,
    db: Session,
    *,
    access: PermissionAdmission,
    work_order_public_id: str,
    purpose: ReplacementWorkspacePurpose,
    candidate_id: str = "",
) -> HTMLResponse:
    try:
        workspace = owner.failed_scope_replacement_workspace(
            db,
            query=FailedScopeReplacementWorkspaceQuery(
                access=access,
                work_order_public_id=work_order_public_id,
                purpose=purpose,
            ),
        )
    except DomainError as exc:
        raise HTTPException(status_code=_status(exc), detail=exc.message) from exc
    return _replacement_render(
        request,
        db,
        page=replacement_page(
            workspace=workspace, purpose=purpose, candidate_id=candidate_id
        ),
    )


@router.get(
    "/dispatch/work-orders/{work_order_public_id}/completion-review/replacement",
    response_class=HTMLResponse,
)
def replacement_review(
    request: Request,
    work_order_public_id: str,
    candidate_id: str = "",
    access: PermissionAdmission = Depends(require_permission_admission(WRITE)),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _replacement_get(
        request,
        db,
        access=access,
        work_order_public_id=work_order_public_id,
        purpose=ReplacementWorkspacePurpose.review,
        candidate_id=candidate_id,
    )


@router.get(
    "/dispatch/work-orders/{work_order_public_id}/completion-review/replacement/revoke",
    response_class=HTMLResponse,
)
def replacement_withdrawal(
    request: Request,
    work_order_public_id: str,
    access: PermissionAdmission = Depends(require_permission_admission(WRITE)),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _replacement_get(
        request,
        db,
        access=access,
        work_order_public_id=work_order_public_id,
        purpose=ReplacementWorkspacePurpose.revoke,
    )


def _replacement_submit(
    request: Request,
    db: Session,
    *,
    access: PermissionAdmission,
    work_order_public_id: str,
    purpose: ReplacementWorkspacePurpose,
) -> HTMLResponse:
    form = parse_form_data_sync(request)
    token = _text(form, "_csrf_token")
    cookie = request.cookies.get(CSRF_COOKIE_NAME)
    if not token or not cookie or not secrets.compare_digest(token, cookie):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
    # Raw values are retained; current workspace must never replace original command tokens.
    values = ReplacementFormValues(
        fields=tuple(
            (key, value)
            for key, value in form.items()
            if isinstance(value, str) and key not in {"_csrf_token", "confirmed"}
        )
    )
    try:
        workspace = owner.failed_scope_replacement_workspace(
            db,
            query=FailedScopeReplacementWorkspaceQuery(
                access=access,
                work_order_public_id=work_order_public_id,
                purpose=purpose,
            ),
        )
    except DomainError as exc:
        return _replacement_render(
            request,
            db,
            page=replacement_unavailable_page(
                purpose=purpose, submitted=values, error=exc.message
            ),
            status_code=_status(exc),
        )
    receipt = None
    error = None
    status = 200
    try:
        source_id = UUID(values.get("source_enrollment_id"))
        if source_id != workspace.source.enrollment_id:
            raise DomainError(
                code="field_completion.stale",
                message="Work order enrollment changed. Reload current information.",
            )
        common = {
            "intent_id": values.get("intent_id"),
            "reason": values.get("reason"),
            "confirmed": form.get("confirmed") == "yes",
            "expected_generation": values.get("expected_generation"),
        }
        if purpose == ReplacementWorkspacePurpose.revoke:
            command = ReplacementRevocationInput.model_validate(
                {**common, "decision_id": values.get("decision_id")}
            ).command(
                access=access,
                source_enrollment_id=source_id,
                work_order_public_id=work_order_public_id,
            )
            db_session_adapter.release_read_transaction(db)
            receipt = owner.revoke_failed_scope_replacement(db, command=command)
        else:
            count = values.get("coverage_count", "0")
            if not count.isdecimal() or not 1 <= int(count) <= 100:
                raise ValueError("Invalid criterion coverage count")
            failure = {"kind": values.get("failure_kind")}
            for key in (
                "event_id",
                "assignment_generation",
                "submission_id",
                "decision_id",
                "review_generation",
            ):
                if values.get(f"failure_{key}"):
                    failure[key] = values.get(f"failure_{key}")
            payload = ReplacementReviewInput.model_validate(
                {
                    **common,
                    **{
                        key: values.get(key)
                        for key in (
                            "replacement_enrollment_id",
                            "source_criteria_revision_id",
                            "replacement_criteria_revision_id",
                            "source_policy_revision_id",
                            "replacement_policy_revision_id",
                            "source_assignment_generation",
                            "replacement_assignment_generation",
                        )
                    },
                    "failure_basis": failure,
                    "coverage": [
                        {
                            "source_criterion_id": values.get(f"coverage_{i}_source"),
                            "replacement_criterion_id": values.get(
                                f"coverage_{i}_replacement"
                            ),
                        }
                        for i in range(int(count))
                    ],
                }
            )
            command_review = payload.command(
                access=access,
                source_enrollment_id=source_id,
                work_order_public_id=work_order_public_id,
            )
            db_session_adapter.release_read_transaction(db)
            receipt = owner.review_failed_scope_replacement(db, command=command_review)
    except (ValidationError, ValueError) as exc:
        error, status = (
            "Check entered values and required confirmation. "
            + str(exc).splitlines()[0],
            422,
        )
    except DomainError as exc:
        status = _status(exc)
        if status in {403, 404}:
            return _replacement_render(
                request,
                db,
                page=replacement_unavailable_page(
                    purpose=purpose, submitted=values, error=exc.message
                ),
                status_code=status,
            )
        error = exc.message
    try:
        workspace = owner.failed_scope_replacement_workspace(
            db,
            query=FailedScopeReplacementWorkspaceQuery(
                access=access,
                work_order_public_id=work_order_public_id,
                purpose=purpose,
            ),
        )
    except DomainError as exc:
        return _replacement_render(
            request,
            db,
            page=replacement_unavailable_page(
                purpose=purpose, submitted=values, error=exc.message
            ),
            status_code=_status(exc),
        )
    return _replacement_render(
        request,
        db,
        page=replacement_page(
            workspace=workspace,
            purpose=purpose,
            candidate_id=values.get("replacement_enrollment_id"),
            submitted=values,
            error=error,
            stale=status == 409,
            receipt=receipt,
        ),
        status_code=status,
    )


@router.post(
    "/dispatch/work-orders/{work_order_public_id}/completion-review/replacement",
    response_class=HTMLResponse,
)
def replacement_decide(
    request: Request,
    work_order_public_id: str,
    access: PermissionAdmission = Depends(require_permission_admission(WRITE)),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _replacement_submit(
        request,
        db,
        access=access,
        work_order_public_id=work_order_public_id,
        purpose=ReplacementWorkspacePurpose.review,
    )


@router.post(
    "/dispatch/work-orders/{work_order_public_id}/completion-review/replacement/revoke",
    response_class=HTMLResponse,
)
def replacement_revoke(
    request: Request,
    work_order_public_id: str,
    access: PermissionAdmission = Depends(require_permission_admission(WRITE)),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _replacement_submit(
        request,
        db,
        access=access,
        work_order_public_id=work_order_public_id,
        purpose=ReplacementWorkspacePurpose.revoke,
    )
