"""Staff request editor over Support's typed readiness and command outcome."""

import json
import secrets
from dataclasses import replace
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.csrf import CSRF_COOKIE_NAME, get_csrf_token, set_csrf_cookie
from app.db import get_db
from app.schemas.support_resolution import (
    ACTION,
    PERMISSION,
    ResolutionFormValues,
    ResolutionRequestInput,
)
from app.services import support
from app.services.action_forms import (
    ActionConfirmation,
    ActionField,
    ActionFieldKind,
    ActionForm,
    ActionHiddenValue,
)
from app.services.auth_dependencies import require_permission_admission
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.form_contracts import SUPPORT_RESOLUTION_FORM
from app.services.permission_admission import PermissionAdmission
from app.services.support_resolution_contracts import (
    SupportResolutionPreview,
    SupportResolutionPreviewQuery,
    SupportResolutionRequestOutcome,
)
from app.services.web_action_readiness import readiness_panel
from app.web.request_parsing import parse_form_data_sync

router = APIRouter(prefix="/support/tickets", tags=["web-admin-support-resolution"])
templates = Jinja2Templates(directory="templates")


def _status(error: DomainError) -> int:
    if error.code.endswith("not_found"):
        return 404
    if error.code.endswith(("denied", "forbidden")):
        return 403
    return 409


def _preview(
    db: Session, *, ticket_id: UUID, access: PermissionAdmission
) -> SupportResolutionPreview:
    try:
        return support.resolution_request_preview(
            db, query=SupportResolutionPreviewQuery(ticket_id=ticket_id, access=access)
        )
    except DomainError as error:
        raise HTTPException(status_code=_status(error), detail=error.message) from error


def _form(
    snapshot: SupportResolutionPreview,
    values: ResolutionFormValues,
    *,
    refused: bool = False,
) -> ActionForm:
    form = ActionForm(
        key=ACTION,
        title="Request customer resolution confirmation",
        description="Ask the customer to confirm the repair or report that the problem remains.",
        action_url=f"/admin/support/tickets/{snapshot.ticket_id}/resolution-confirmation",
        submit_label="Request customer confirmation",
        fields=(
            ActionField(
                key="grace_hours",
                label="Grace period (hours)",
                kind=ActionFieldKind.decimal,
                value=values.grace_hours,
                required=True,
                min_value="0",
                step="1",
                help_text="After this period the request-bound timer rechecks current completion evidence before automatic closure.",
            ),
            ActionField(
                key="reason",
                label="Reason for this request",
                kind=ActionFieldKind.textarea,
                value=values.reason,
                required=True,
                max_length=1000,
            ),
        ),
        hidden_values=(
            *(
                (ActionHiddenValue("intent_id", values.intent_id),)
                if values.intent_id.strip()
                else ()
            ),
            ActionHiddenValue("reviewed_shared_scopes", values.reviewed_shared_scopes),
        ),
        impact="Records an immutable request and its completion evidence basis. A new request replaces the previous customer confirmation capability.",
        confirmation=ActionConfirmation(
            "Confirm this request",
            "I reviewed every displayed shared incident restoration and every issued field scope, including any explicit restored-without-issued-field-scope review. The ticket moves to pending customer confirmation.",
        ),
    ).gated_by(snapshot.readiness)
    return (
        replace(
            form,
            allowed=False,
            disabled_reason="Original intent and values are preserved. Reload current readiness to start a new intent.",
        )
        if refused
        else form
    )


def _render(
    request: Request,
    db: Session,
    *,
    snapshot: SupportResolutionPreview,
    values: ResolutionFormValues,
    error: str | None = None,
    receipt: SupportResolutionRequestOutcome | None = None,
    status_code: int = 200,
    refused: bool = False,
) -> HTMLResponse:
    from app.web.admin import get_current_user, get_sidebar_stats

    token = get_csrf_token(request)
    response = templates.TemplateResponse(
        request=request,
        name="admin/support/tickets/resolution_confirmation.html",
        context={
            "request": request,
            "snapshot": snapshot,
            "form": _form(snapshot, values, refused=refused)
            if receipt is None
            else None,
            "panel": readiness_panel(snapshot.readiness),
            "contract": SUPPORT_RESOLUTION_FORM,
            "receipt": receipt,
            "error": error,
            "refused": refused,
            "csrf_token": token,
            "active_page": "support-tickets",
            "active_menu": "services",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
        status_code=status_code,
    )
    set_csrf_cookie(response, token, request)
    return response


@router.get("/{ticket_id}/resolution-confirmation", response_class=HTMLResponse)
def editor(
    request: Request,
    ticket_id: UUID,
    access: PermissionAdmission = Depends(require_permission_admission(PERMISSION)),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    snapshot = _preview(db, ticket_id=ticket_id, access=access)
    return _render(
        request,
        db,
        snapshot=snapshot,
        values=ResolutionFormValues(
            str(uuid4()),
            reviewed_shared_scopes=json.dumps(
                [
                    {
                        "complaint_link_id": str(scope.complaint_link_id),
                        "reviewed_revision_id": str(scope.reviewed_revision_id),
                        "membership_token": scope.membership_token,
                        "resolved_at": scope.resolved_at.isoformat(),
                        "issuance_link_ids": [
                            str(x.issuance_link_id) for x in scope.issuances
                        ],
                        "empty_scope_review": "restored_without_issued_field_scope"
                        if not scope.issuances
                        else None,
                    }
                    for scope in snapshot.shared_scopes
                ]
            ),
        ),
    )


@router.post("/{ticket_id}/resolution-confirmation", response_class=HTMLResponse)
def submit(
    request: Request,
    ticket_id: UUID,
    access: PermissionAdmission = Depends(require_permission_admission(PERMISSION)),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    form = parse_form_data_sync(request)
    token, cookie = form.get("_csrf_token"), request.cookies.get(CSRF_COOKIE_NAME)
    if (
        not isinstance(token, str)
        or not cookie
        or not secrets.compare_digest(token, cookie)
    ):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    def text(name: str) -> str:
        value = form.get(name)
        return value if isinstance(value, str) else ""

    values = ResolutionFormValues(
        text("intent_id"),
        text("reason"),
        text("grace_hours"),
        text("reviewed_shared_scopes") or "[]",
    )
    try:
        payload = ResolutionRequestInput.model_validate(
            {
                **{key: value for key, value in form.items() if key != "_csrf_token"},
                "confirmed": text("confirmed") == "yes",
                "reviewed_shared_scopes": json.loads(values.reviewed_shared_scopes),
            }
        )
    except (ValidationError, ValueError) as error:
        invalid_intent = (
            isinstance(error, ValueError)
            and not isinstance(error, ValidationError)
            or isinstance(error, ValidationError)
            and any(item["loc"] == ("intent_id",) for item in error.errors())
        )
        snapshot = _preview(db, ticket_id=ticket_id, access=access)
        return _render(
            request,
            db,
            snapshot=snapshot,
            values=values,
            error="Enter a valid original intent, reason, whole-hour grace period and confirmation.",
            status_code=422,
            refused=invalid_intent,
        )
    command = payload.command(ticket_id=ticket_id, access=access)
    db_session_adapter.release_read_transaction(db)
    try:
        receipt = support.request_resolution(db, command=command)
    except DomainError as error:
        snapshot = _preview(db, ticket_id=ticket_id, access=access)
        return _render(
            request,
            db,
            snapshot=snapshot,
            values=values,
            error=error.message,
            status_code=_status(error),
            refused=True,
        )
    snapshot = _preview(db, ticket_id=ticket_id, access=access)
    return _render(request, db, snapshot=snapshot, values=values, receipt=receipt)
