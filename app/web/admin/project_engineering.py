"""Staff browser adapters over typed engineering reads and atomic commands."""

import secrets
from dataclasses import replace
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.csrf import CSRF_COOKIE_NAME, get_csrf_token, set_csrf_cookie
from app.db import get_db
from app.schemas.project_engineering import (
    EngineeringDraftInput,
    EngineeringFormAction,
    EngineeringFormValues,
    EngineeringIssuanceInput,
    EngineeringPolicyInput,
    EngineeringReviewInput,
    EngineeringRevocationInput,
    browser_input,
)
from app.services import project_engineering_plans as owner
from app.services import project_work_order_handoff
from app.services.auth_dependencies import require_permission_admission
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.permission_admission import PermissionAdmission
from app.services.project_engineering_plan_contracts import (
    APPROVE,
    POLICY,
    READ,
    REVOKE,
    WRITE,
    EngineeringReadPurpose,
    EngineeringTechnology,
    EngineeringWorkspaceQuery,
    ProjectDispatchPreviewQuery,
)
from app.services.project_engineering_projection import (
    ProjectEngineeringPage,
    project_page,
)
from app.services.project_work_order_handoff_contracts import PERMISSION
from app.web.request_parsing import parse_form_data_sync

router = APIRouter(
    prefix="/projects/{project_id}/engineering", tags=["web-admin-project-engineering"]
)
templates = Jinja2Templates(directory="templates")


def _status(error: DomainError) -> int:
    if error.code.endswith((".denied", ".unauthorized", "_forbidden")):
        return 403
    if error.code.endswith((".not_found", "_not_found")):
        return 404
    if error.code.endswith(
        (
            ".stale_state",
            ".unapproved",
            ".scope_mismatch",
            ".idempotency_conflict",
            ".relationship_conflict",
        )
    ):
        return 409
    return 422


def _page(
    db: Session,
    *,
    access: PermissionAdmission,
    project_id: UUID,
    task_id: UUID | None,
    technology: EngineeringTechnology,
    purpose: EngineeringReadPurpose | EngineeringFormAction,
    history_offset: int = 0,
    selected_revision_id: UUID | None = None,
    submitted: EngineeringFormValues | None = None,
    error: str | None = None,
    stale: bool = False,
) -> ProjectEngineeringPage:
    try:
        if purpose == EngineeringFormAction.issue:
            preview = owner.preview_dispatch(
                db,
                query=ProjectDispatchPreviewQuery(
                    access=access,
                    project_id=project_id,
                    task_id=task_id,
                    technology=technology,
                ),
            )
            page = project_page(
                preview=preview, submitted=submitted, error=error, stale=stale
            )
        else:
            workspace = owner.read_workspace(
                db,
                query=EngineeringWorkspaceQuery(
                    access=access,
                    project_id=project_id,
                    task_id=task_id,
                    technology=technology,
                    purpose=EngineeringReadPurpose(purpose.value),
                    history_offset=history_offset,
                    selected_revision_id=selected_revision_id,
                ),
            )
            page = project_page(
                workspace=workspace, submitted=submitted, error=error, stale=stale
            )
        if stale and page.form:
            page = replace(
                page,
                form=replace(
                    page.form,
                    allowed=False,
                    disabled_reason="Reload current evidence before submitting a new intent.",
                ),
            )
        return page
    except DomainError as exc:
        raise HTTPException(status_code=_status(exc), detail=exc.message) from exc


def _render(
    request: Request,
    db: Session,
    *,
    page: ProjectEngineeringPage,
    status_code: int = 200,
) -> HTMLResponse:
    from app.web.admin import get_current_user, get_sidebar_stats

    token = get_csrf_token(request)
    response = templates.TemplateResponse(
        request=request,
        name="admin/projects/engineering/page.html",
        context={
            "request": request,
            "page": page,
            "active_page": "projects",
            "active_menu": "operations",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "csrf_token": token,
        },
        status_code=status_code,
    )
    set_csrf_cookie(response, token, request)
    return response


def _submit(
    request: Request,
    db: Session,
    *,
    access: PermissionAdmission,
    project_id: UUID,
    task_id: UUID | None,
    technology: EngineeringTechnology,
    action: EngineeringFormAction,
) -> HTMLResponse | RedirectResponse:
    form = parse_form_data_sync(request)
    csrf = form.get("_csrf_token")
    cookie = request.cookies.get(CSRF_COOKIE_NAME)
    if (
        not isinstance(csrf, str)
        or not cookie
        or not secrets.compare_digest(csrf, cookie)
    ):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
    values = EngineeringFormValues.model_validate(
        {
            name: value if isinstance(value := form.get(name), str) else ""
            for name in EngineeringFormValues.model_fields
        }
    )
    page = _page(
        db,
        access=access,
        project_id=project_id,
        task_id=task_id,
        technology=technology,
        purpose=action,
        submitted=values,
    )
    try:
        payload = browser_input(
            action=action,
            values=values,
            confirmed=form.get("confirmed") == "yes",
            workspace=page.workspace,
            preview=page.preview,
        )
        db_session_adapter.release_read_transaction(db)
        if isinstance(payload, EngineeringPolicyInput):
            owner.publish_policy(
                db, command=payload.command(access=access, project_id=project_id)
            )
        elif isinstance(payload, EngineeringDraftInput):
            owner.draft_revision(
                db,
                command=payload.command(
                    access=access, project_id=project_id, task_id=task_id
                ),
            )
        elif isinstance(payload, (EngineeringReviewInput, EngineeringRevocationInput)):
            owner.decide(
                db,
                command=payload.command(
                    access=access, project_id=project_id, task_id=task_id
                ),
            )
        elif isinstance(payload, EngineeringIssuanceInput):
            receipt = project_work_order_handoff.issue(
                db,
                command=payload.command(
                    access=access, project_id=project_id, task_id=task_id
                ),
            )
            return _render(
                request,
                db,
                page=replace(
                    page,
                    issued=receipt,
                    form=None,
                    title="Infrastructure work order issued",
                ),
            )
    except (ValidationError, ValueError, ArithmeticError) as exc:
        message = (
            "Check the entered values and required confirmation. "
            + str(exc).splitlines()[0]
        )
        return _render(
            request,
            db,
            page=_page(
                db,
                access=access,
                project_id=project_id,
                task_id=task_id,
                technology=technology,
                purpose=action,
                submitted=values,
                error=message,
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
                project_id=project_id,
                task_id=task_id,
                technology=technology,
                purpose=action,
                submitted=values,
                error=exc.message,
                stale=status == 409,
            ),
            status_code=status,
        )
    return RedirectResponse(
        str(request.url.include_query_params(saved="yes")), status_code=303
    )


@router.get("", response_class=HTMLResponse)
def overview(
    request: Request,
    project_id: UUID,
    task_id: UUID | None = None,
    technology: EngineeringTechnology = EngineeringTechnology.fiber,
    history_offset: int = 0,
    selected_revision_id: UUID | None = None,
    access: PermissionAdmission = Depends(require_permission_admission(READ)),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _render(
        request,
        db,
        page=_page(
            db,
            access=access,
            project_id=project_id,
            task_id=task_id,
            technology=technology,
            purpose=EngineeringReadPurpose.overview,
            history_offset=history_offset,
            selected_revision_id=selected_revision_id,
        ),
    )


@router.get("/policy", response_class=HTMLResponse)
def policy(
    request: Request,
    project_id: UUID,
    task_id: UUID | None = None,
    technology: EngineeringTechnology = EngineeringTechnology.fiber,
    history_offset: int = 0,
    access: PermissionAdmission = Depends(require_permission_admission(POLICY)),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _render(
        request,
        db,
        page=_page(
            db,
            access=access,
            project_id=project_id,
            task_id=None,
            technology=technology,
            purpose=EngineeringReadPurpose.policy,
            history_offset=history_offset,
        ),
    )


@router.post("/policy", response_model=None)
def submit_policy(
    request: Request,
    project_id: UUID,
    task_id: UUID | None = None,
    technology: EngineeringTechnology = EngineeringTechnology.fiber,
    access: PermissionAdmission = Depends(require_permission_admission(POLICY)),
    db: Session = Depends(get_db),
) -> HTMLResponse | RedirectResponse:
    return _submit(
        request,
        db,
        access=access,
        project_id=project_id,
        task_id=None,
        technology=technology,
        action=EngineeringFormAction.policy,
    )


@router.get("/draft", response_class=HTMLResponse)
def draft(
    request: Request,
    project_id: UUID,
    task_id: UUID | None = None,
    technology: EngineeringTechnology = EngineeringTechnology.fiber,
    history_offset: int = 0,
    access: PermissionAdmission = Depends(require_permission_admission(WRITE)),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _render(
        request,
        db,
        page=_page(
            db,
            access=access,
            project_id=project_id,
            task_id=task_id,
            technology=technology,
            purpose=EngineeringReadPurpose.draft,
            history_offset=history_offset,
        ),
    )


@router.post("/draft", response_model=None)
def submit_draft(
    request: Request,
    project_id: UUID,
    task_id: UUID | None = None,
    technology: EngineeringTechnology = EngineeringTechnology.fiber,
    access: PermissionAdmission = Depends(require_permission_admission(WRITE)),
    db: Session = Depends(get_db),
) -> HTMLResponse | RedirectResponse:
    return _submit(
        request,
        db,
        access=access,
        project_id=project_id,
        task_id=task_id,
        technology=technology,
        action=EngineeringFormAction.draft,
    )


@router.get("/review", response_class=HTMLResponse)
def review(
    request: Request,
    project_id: UUID,
    task_id: UUID | None = None,
    technology: EngineeringTechnology = EngineeringTechnology.fiber,
    history_offset: int = 0,
    access: PermissionAdmission = Depends(require_permission_admission(APPROVE)),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _render(
        request,
        db,
        page=_page(
            db,
            access=access,
            project_id=project_id,
            task_id=task_id,
            technology=technology,
            purpose=EngineeringReadPurpose.review,
            history_offset=history_offset,
        ),
    )


@router.post("/review", response_model=None)
def submit_review(
    request: Request,
    project_id: UUID,
    task_id: UUID | None = None,
    technology: EngineeringTechnology = EngineeringTechnology.fiber,
    access: PermissionAdmission = Depends(require_permission_admission(APPROVE)),
    db: Session = Depends(get_db),
) -> HTMLResponse | RedirectResponse:
    return _submit(
        request,
        db,
        access=access,
        project_id=project_id,
        task_id=task_id,
        technology=technology,
        action=EngineeringFormAction.review,
    )


@router.get("/revoke", response_class=HTMLResponse)
def revoke(
    request: Request,
    project_id: UUID,
    task_id: UUID | None = None,
    technology: EngineeringTechnology = EngineeringTechnology.fiber,
    history_offset: int = 0,
    access: PermissionAdmission = Depends(require_permission_admission(REVOKE)),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _render(
        request,
        db,
        page=_page(
            db,
            access=access,
            project_id=project_id,
            task_id=task_id,
            technology=technology,
            purpose=EngineeringReadPurpose.revoke,
            history_offset=history_offset,
        ),
    )


@router.post("/revoke", response_model=None)
def submit_revoke(
    request: Request,
    project_id: UUID,
    task_id: UUID | None = None,
    technology: EngineeringTechnology = EngineeringTechnology.fiber,
    access: PermissionAdmission = Depends(require_permission_admission(REVOKE)),
    db: Session = Depends(get_db),
) -> HTMLResponse | RedirectResponse:
    return _submit(
        request,
        db,
        access=access,
        project_id=project_id,
        task_id=task_id,
        technology=technology,
        action=EngineeringFormAction.revoke,
    )


@router.get("/issuance", response_class=HTMLResponse)
def issuance(
    request: Request,
    project_id: UUID,
    task_id: UUID | None = None,
    technology: EngineeringTechnology = EngineeringTechnology.fiber,
    history_offset: int = 0,
    access: PermissionAdmission = Depends(require_permission_admission(PERMISSION)),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _render(
        request,
        db,
        page=_page(
            db,
            access=access,
            project_id=project_id,
            task_id=task_id,
            technology=technology,
            purpose=EngineeringFormAction.issue,
            history_offset=history_offset,
        ),
    )


@router.post("/issuance", response_model=None)
def submit_issuance(
    request: Request,
    project_id: UUID,
    task_id: UUID | None = None,
    technology: EngineeringTechnology = EngineeringTechnology.fiber,
    access: PermissionAdmission = Depends(require_permission_admission(PERMISSION)),
    db: Session = Depends(get_db),
) -> HTMLResponse | RedirectResponse:
    return _submit(
        request,
        db,
        access=access,
        project_id=project_id,
        task_id=task_id,
        technology=technology,
        action=EngineeringFormAction.issue,
    )
