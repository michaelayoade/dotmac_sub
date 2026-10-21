"""Staff reviewed followups; exact source, explicit responsibility and due time."""

import secrets
from datetime import datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.csrf import CSRF_COOKIE_NAME, get_csrf_token, set_csrf_cookie
from app.db import get_db
from app.services.auth_dependencies import require_permission_admission
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.field import visit_followup as owner
from app.services.field.completion_review_contracts import READ as COMPLETION_READ
from app.services.field.completion_review_contracts import WRITE as COMPLETION_WRITE
from app.services.field.visit_followup_contracts import (
    READ,
    WRITE,
    ApprovedFollowupReplacementQuery,
    DecideVisitFollowup,
    FailedVisitIntakeQuery,
    FailedVisitSourceQuery,
    FailureSource,
    FailureSourceKind,
    FollowupDisposition,
    FollowupResponsibility,
    FollowupResponsibilityOptionsQuery,
    OpenVisitFollowup,
    VisitFollowupQueueQuery,
    VisitFollowupWorkspaceQuery,
)
from app.services.form_contracts import FormConsequence, FormContract, register
from app.services.owner_commands import CommandContext
from app.services.permission_admission import PermissionAdmission
from app.web.request_parsing import parse_form_data_sync

router = APIRouter(
    prefix="/dispatch/visit-followups", tags=["web-admin-visit-followups"]
)
templates = Jinja2Templates(directory="templates")
FORM = register(
    FormContract(
        key="field_visit_followup.review",
        title="Record accountable followup",
        entity="failed_visit",
        command_owner="operations.field_visit_followup",
        consequences=(
            FormConsequence(
                "record",
                "Records responsible team, explicit due time and a review disposition. Revisit required remains pending issuance until an approved replacement is explicitly linked.",
            ),
        ),
    )
)


def _status(error: DomainError) -> int:
    if "denied" in error.code:
        return 403
    if error.code.endswith("not_found"):
        return 404
    return 400 if error.code.endswith("invalid_request") else 409


def _render(
    request: Request,
    db: Session,
    access: PermissionAdmission,
    completion: PermissionAdmission,
    *,
    obligation_id: UUID | None = None,
    submitted: dict[str, str] | None = None,
    error: str | None = None,
    status: int = 200,
    replacement_access: PermissionAdmission | None = None,
) -> HTMLResponse:
    from app.web.admin import get_current_user, get_sidebar_stats

    values = submitted or {}
    try:
        selected_source_id = values.get("source_id") or request.query_params.get(
            "source_id"
        )
        selected_source_kind = values.get("source_kind") or request.query_params.get(
            "source_kind"
        )
        try:
            selected_source = (
                owner.pending_source(
                    db,
                    query=FailedVisitSourceQuery(
                        access,
                        completion,
                        FailureSource(
                            FailureSourceKind(selected_source_kind),
                            UUID(selected_source_id),
                        ),
                    ),
                )
                if obligation_id is None and selected_source_id and selected_source_kind
                else None
            )
        except DomainError as exc:
            if status != 409 or not submitted or _status(exc) == 403:
                raise
            selected_source = None
        team_id = (
            UUID(values.get("team_id") or request.query_params["team_id"])
            if values.get("team_id") or request.query_params.get("team_id")
            else None
        )
        workspace = (
            owner.workspace(
                db, query=VisitFollowupWorkspaceQuery(access, completion, obligation_id)
            )
            if obligation_id
            else None
        )
        try:
            approved_option = (
                owner.approved_replacement_option(
                    db,
                    query=ApprovedFollowupReplacementQuery(
                        access, completion, replacement_access, obligation_id
                    ),
                )
                if obligation_id and replacement_access
                else None
            )
        except DomainError as exc:
            if status != 409 or not submitted or _status(exc) == 403:
                raise
            approved_option = None
        if workspace and team_id is None:
            team_id = workspace.revisions[0].responsible_team_id
        options = owner.responsibility_options(
            db, query=FollowupResponsibilityOptionsQuery(access, completion, team_id)
        )
        pending = (
            None
            if workspace
            else owner.pending_failures(
                db,
                query=FailedVisitIntakeQuery(
                    access,
                    completion,
                    UUID(request.query_params["after_source"])
                    if request.query_params.get("after_source")
                    else None,
                ),
            )
        )
        queue = (
            None
            if workspace
            else owner.followup_queue(
                db,
                query=VisitFollowupQueueQuery(
                    access,
                    completion,
                    UUID(request.query_params["after_followup"])
                    if request.query_params.get("after_followup")
                    else None,
                    include_closed=request.query_params.get("history") == "1",
                ),
            )
        )
    except ValueError as exc:
        raise HTTPException(400, "Invalid followup selection") from exc
    except DomainError as exc:
        raise HTTPException(_status(exc), exc.message) from exc
    token = get_csrf_token(request)
    response = templates.TemplateResponse(
        request=request,
        name="admin/dispatch/visit_followups.html",
        context={
            "request": request,
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "active_page": "dispatch",
            "workspace": workspace,
            "approved_link_page": replacement_access is not None,
            "approved_option": approved_option,
            "selected_source": selected_source,
            "pending": pending,
            "queue": queue,
            "options": options,
            "selected_team": str(team_id) if team_id else "",
            "submitted": values,
            "error": error,
            "stale": status == 409,
            "intent_key": values.get("intent_key", str(uuid4())),
            "form_contract": FORM,
            "csrf_token": token,
        },
        status_code=status,
    )
    set_csrf_cookie(response, token)
    return response


@router.get("", response_class=HTMLResponse)
def followup_intake(
    request: Request,
    db: Session = Depends(get_db),
    access: PermissionAdmission = Depends(require_permission_admission(READ)),
    completion: PermissionAdmission = Depends(
        require_permission_admission(COMPLETION_READ)
    ),
) -> HTMLResponse:
    return _render(request, db, access, completion)


@router.get("/{obligation_id}", response_class=HTMLResponse)
def followup_detail(
    request: Request,
    obligation_id: UUID,
    db: Session = Depends(get_db),
    access: PermissionAdmission = Depends(require_permission_admission(READ)),
    completion: PermissionAdmission = Depends(
        require_permission_admission(COMPLETION_READ)
    ),
) -> HTMLResponse:
    return _render(request, db, access, completion, obligation_id=obligation_id)


def _submit(
    request: Request,
    db: Session,
    access: PermissionAdmission,
    read: PermissionAdmission,
    completion: PermissionAdmission,
    *,
    obligation_id: UUID | None = None,
    replacement_access: PermissionAdmission | None = None,
) -> HTMLResponse | RedirectResponse:
    raw = parse_form_data_sync(request)
    token, cookie = raw.get("_csrf_token"), request.cookies.get(CSRF_COOKIE_NAME)
    if (
        not isinstance(token, str)
        or not cookie
        or not secrets.compare_digest(token, cookie)
    ):
        raise HTTPException(403, "Invalid CSRF token")
    values = {
        key: str(raw.get(key, ""))
        for key in (
            "intent_key",
            "source_kind",
            "source_id",
            "revision_id",
            "team_id",
            "person_id",
            "due_at",
            "reason",
            "disposition",
            "replacement_decision_id",
            "replacement_generation",
        )
    }
    try:
        responsibility = FollowupResponsibility(
            UUID(values["team_id"]),
            UUID(values["person_id"]) if values["person_id"] else None,
            datetime.fromisoformat(values["due_at"].replace("Z", "+00:00")),
        )
        context = CommandContext.system(
            actor=str(access.principal_id),
            scope=WRITE,
            reason=values["reason"],
            idempotency_key=values["intent_key"],
        )
        # A separate route with an explicit completion-WRITE dependency links replacements.
        if values["replacement_decision_id"] and replacement_access is None:
            raise HTTPException(403, "Use the approved replacement linkage action")
        command = (
            DecideVisitFollowup(
                context,
                access,
                obligation_id,
                UUID(values["revision_id"]),
                responsibility,
                FollowupDisposition(values["disposition"]),
                values["reason"],
                UUID(values["replacement_decision_id"])
                if values["replacement_decision_id"]
                else None,
                completion,
                replacement_access,
                int(values["replacement_generation"])
                if values["replacement_generation"]
                else None,
            )
            if obligation_id
            else OpenVisitFollowup(
                context,
                access,
                FailureSource(
                    FailureSourceKind(values["source_kind"]), UUID(values["source_id"])
                ),
                responsibility,
                values["reason"],
                completion,
            )
        )
        db_session_adapter.release_read_transaction(db)
        outcome = (
            owner.decide_followup(db, command=command)
            if isinstance(command, DecideVisitFollowup)
            else owner.open_followup(db, command=command)
        )
    except ValueError:
        return _render(
            request,
            db,
            read,
            completion,
            obligation_id=obligation_id,
            submitted=values,
            error="Choose exact responsibility and a timezone-aware due time (for example 2026-10-12T09:00:00+01:00).",
            status=400,
            replacement_access=replacement_access,
        )
    except DomainError as exc:
        if _status(exc) == 403:
            raise HTTPException(403, exc.message) from exc
        return _render(
            request,
            db,
            read,
            completion,
            obligation_id=obligation_id,
            submitted=values,
            error=exc.message,
            status=_status(exc),
            replacement_access=replacement_access,
        )
    return RedirectResponse(
        f"/admin/dispatch/visit-followups/{outcome.obligation_id}", status_code=303
    )


@router.post("", response_model=None)
def followup_open(
    request: Request,
    db: Session = Depends(get_db),
    access: PermissionAdmission = Depends(require_permission_admission(WRITE)),
    read: PermissionAdmission = Depends(require_permission_admission(READ)),
    completion: PermissionAdmission = Depends(
        require_permission_admission(COMPLETION_READ)
    ),
) -> HTMLResponse | RedirectResponse:
    return _submit(request, db, access, read, completion)


@router.post("/{obligation_id}", response_model=None)
def followup_decide(
    request: Request,
    obligation_id: UUID,
    db: Session = Depends(get_db),
    access: PermissionAdmission = Depends(require_permission_admission(WRITE)),
    read: PermissionAdmission = Depends(require_permission_admission(READ)),
    completion: PermissionAdmission = Depends(
        require_permission_admission(COMPLETION_READ)
    ),
) -> HTMLResponse | RedirectResponse:
    return _submit(request, db, access, read, completion, obligation_id=obligation_id)


@router.post("/{obligation_id}/approved-replacement", response_model=None)
def followup_link_replacement(
    request: Request,
    obligation_id: UUID,
    db: Session = Depends(get_db),
    access: PermissionAdmission = Depends(require_permission_admission(WRITE)),
    read: PermissionAdmission = Depends(require_permission_admission(READ)),
    completion: PermissionAdmission = Depends(
        require_permission_admission(COMPLETION_READ)
    ),
    replacement: PermissionAdmission = Depends(
        require_permission_admission(COMPLETION_WRITE)
    ),
) -> HTMLResponse | RedirectResponse:
    return _submit(
        request,
        db,
        access,
        read,
        completion,
        obligation_id=obligation_id,
        replacement_access=replacement,
    )


@router.get("/{obligation_id}/approved-replacement", response_class=HTMLResponse)
def followup_approved_replacement(
    request: Request,
    obligation_id: UUID,
    db: Session = Depends(get_db),
    access: PermissionAdmission = Depends(require_permission_admission(READ)),
    completion: PermissionAdmission = Depends(
        require_permission_admission(COMPLETION_READ)
    ),
    replacement: PermissionAdmission = Depends(
        require_permission_admission(COMPLETION_WRITE)
    ),
) -> HTMLResponse:
    return _render(
        request,
        db,
        access,
        completion,
        obligation_id=obligation_id,
        replacement_access=replacement,
    )
