"""Browser adapters for the typed native work-order classification owner."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from urllib.parse import urlencode
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.csrf import (
    CSRF_COOKIE_NAME,
    CSRFValidationError,
    get_csrf_token,
    set_csrf_cookie,
)
from app.db import get_db
from app.services.auth_dependencies import require_permission
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext
from app.services.work_order_classification_contracts import (
    ClassifyWorkOrderCommand,
    WorkOrderClassificationQuery,
    WorkOrderClassificationSnapshot,
)
from app.services.work_order_commands import work_order_commands
from app.services.work_order_errors import WorkOrderCommandError
from app.services.work_order_templates import (
    ExplicitWorkOrderClassification,
    WorkOrderAccessTechnology,
    WorkOrderPurpose,
    WorkOrderTemplateError,
)
from app.web.request_parsing import parse_form_data_sync

PERMISSION = "operations:dispatch:write"
router = APIRouter(
    prefix="/dispatch/work-orders", tags=["web-admin-work-order-classification"]
)
templates = Jinja2Templates(directory="templates")


class ClassificationFormField(StrEnum):
    PURPOSE = "purpose"
    TECHNOLOGY = "access_technology"
    REASON = "reason"
    REVISION = "expected_revision"
    COMMAND_ID = "command_id"


@dataclass(frozen=True, kw_only=True)
class ClassificationFormValues:
    command_id: str
    expected_revision: str
    purpose: str
    access_technology: str
    reason: str


@dataclass(frozen=True, kw_only=True)
class ClassificationPage:
    snapshot: WorkOrderClassificationSnapshot
    form: ClassificationFormValues
    error: str | None = None
    error_field: ClassificationFormField | None = None
    stale: bool = False


class ClassificationFormError(Exception):
    def __init__(self, *, field: ClassificationFormField, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


def _text(form: FormData, name: str) -> str:
    value = form.get(name)
    return value if isinstance(value, str) else ""


def _snapshot(db: Session, identity: UUID) -> WorkOrderClassificationSnapshot:
    try:
        return work_order_commands.classification_snapshot(
            db, query=WorkOrderClassificationQuery(work_order_id=identity)
        )
    except WorkOrderCommandError as exc:
        raise HTTPException(
            status_code=404 if exc.kind == "not_found" else 409, detail=exc.message
        ) from exc


def _render(
    request: Request, db: Session, *, page: ClassificationPage, status_code: int = 200
) -> HTMLResponse:
    from app.web.admin import get_current_user, get_sidebar_stats

    csrf_token = get_csrf_token(request)
    response = templates.TemplateResponse(
        request=request,
        name="admin/dispatch/work_order_classification.html",
        context={
            "request": request,
            "active_page": "dispatch-work-orders",
            "active_menu": "operations",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "page": page,
            "csrf_token": csrf_token,
        },
        status_code=status_code,
    )
    set_csrf_cookie(response, csrf_token, request)
    return response


def _command(
    values: ClassificationFormValues, *, identity: UUID, actor: str
) -> ClassifyWorkOrderCommand:
    try:
        command_id = UUID(values.command_id)
    except ValueError as exc:
        raise ClassificationFormError(
            field=ClassificationFormField.COMMAND_ID,
            message="Reload the form to obtain a valid operation identity.",
        ) from exc
    try:
        revision = datetime.fromisoformat(values.expected_revision)
        if revision.tzinfo is None or revision.utcoffset() is None:
            raise ValueError("Aware revision required")
    except ValueError as exc:
        raise ClassificationFormError(
            field=ClassificationFormField.REVISION,
            message="Reload the form to obtain the current work-order revision.",
        ) from exc
    try:
        purpose = WorkOrderPurpose(values.purpose)
    except ValueError as exc:
        raise ClassificationFormError(
            field=ClassificationFormField.PURPOSE,
            message="Select a governed work purpose.",
        ) from exc
    try:
        technology = WorkOrderAccessTechnology(values.access_technology)
    except ValueError as exc:
        raise ClassificationFormError(
            field=ClassificationFormField.TECHNOLOGY,
            message="Select fiber, radio or unknown access technology.",
        ) from exc
    reason = values.reason.strip()
    if not reason or len(reason) > 1000:
        raise ClassificationFormError(
            field=ClassificationFormField.REASON,
            message="Enter a reason of 1 to 1,000 characters.",
        )
    return ClassifyWorkOrderCommand(
        context=CommandContext(
            command_id=command_id,
            correlation_id=command_id,
            actor=actor,
            scope=PERMISSION,
            reason=reason,
            idempotency_key=str(command_id),
        ),
        work_order_id=identity,
        expected_revision=revision,
        classification=ExplicitWorkOrderClassification(
            purpose=purpose, access_technology=technology
        ),
    )


@router.get("/{work_order_id}/classification", response_class=HTMLResponse)
def classification_page(
    request: Request,
    work_order_id: UUID,
    auth: dict = Depends(require_permission(PERMISSION)),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    snapshot = _snapshot(db, work_order_id)
    classification = snapshot.classification
    return _render(
        request,
        db,
        page=ClassificationPage(
            snapshot=snapshot,
            form=ClassificationFormValues(
                command_id=str(uuid4()),
                expected_revision=snapshot.revision.isoformat(),
                purpose=classification.purpose.value if classification else "",
                access_technology=classification.access_technology.value
                if classification
                else "",
                reason="",
            ),
        ),
    )


@router.post(
    "/{work_order_id}/classification", response_class=HTMLResponse, response_model=None
)
def classify_from_form(
    request: Request,
    work_order_id: UUID,
    auth: dict = Depends(require_permission(PERMISSION)),
    db: Session = Depends(get_db),
) -> HTMLResponse | RedirectResponse:
    raw = parse_form_data_sync(request)
    csrf_cookie = request.cookies.get(CSRF_COOKIE_NAME)
    csrf_submitted = _text(raw, "_csrf_token")
    if (
        not csrf_cookie
        or not csrf_submitted
        or not secrets.compare_digest(csrf_cookie.encode(), csrf_submitted.encode())
    ):
        raise CSRFValidationError()
    values = ClassificationFormValues(
        command_id=_text(raw, "command_id"),
        expected_revision=_text(raw, "expected_revision"),
        purpose=_text(raw, "purpose"),
        access_technology=_text(raw, "access_technology"),
        reason=_text(raw, "reason"),
    )
    try:
        command = _command(
            values,
            identity=work_order_id,
            actor=str(auth["principal_id"]),
        )
    except ClassificationFormError as exc:
        return _render(
            request,
            db,
            status_code=422,
            page=ClassificationPage(
                snapshot=_snapshot(db, work_order_id),
                form=values,
                error=exc.message,
                error_field=exc.field,
            ),
        )
    db_session_adapter.release_read_transaction(db)
    try:
        outcome = work_order_commands.classify(db, command=command)
    except WorkOrderTemplateError as exc:
        return _render(
            request,
            db,
            status_code=422,
            page=ClassificationPage(
                snapshot=_snapshot(db, work_order_id),
                form=values,
                error=exc.message,
            ),
        )
    except WorkOrderCommandError as exc:
        if exc.kind == "not_found":
            raise HTTPException(status_code=404, detail=exc.message) from exc
        return _render(
            request,
            db,
            status_code=409,
            page=ClassificationPage(
                snapshot=_snapshot(db, work_order_id),
                form=values,
                error=exc.message,
                stale=exc.code == "stale_classification",
            ),
        )
    notice = urlencode(
        {
            "notice": "Work-order classification saved. Planning requirements remain recommendations."
        }
    )
    return RedirectResponse(
        url=f"/admin/dispatch/work-orders/{outcome.public_id}?{notice}", status_code=303
    )
