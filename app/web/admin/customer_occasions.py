"""Permission-gated adapters for reviewing customer occasion automations."""

from datetime import UTC, datetime
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import EmailStr, ValidationError
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import customer_occasions
from app.services.auth_dependencies import (
    has_permission,
    require_permission,
    require_user_auth,
)
from app.services.customer_occasion_contracts import (
    READ_PERMISSION,
    TEST_PERMISSION,
    UPDATE_PERMISSION,
    CustomerOccasion,
    OccasionReviewQuery,
    OccasionSettings,
    SaveOccasionCommand,
    TestOccasionCommand,
)
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext

router = APIRouter(prefix="/occasions", tags=["customer-occasion-automations"])
templates = Jinja2Templates(directory="templates")


@router.get(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission(READ_PERMISSION))],
)
def occasion_review(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    from app.web.admin import get_current_user, get_sidebar_stats

    auth = require_user_auth(request=request, db=db)
    return templates.TemplateResponse(
        "admin/notifications/occasions.html",
        {
            "request": request,
            "review": customer_occasions.review(
                db=db, query=OccasionReviewQuery(now=datetime.now(UTC))
            ),
            "can_write": has_permission(auth, db, UPDATE_PERMISSION),
            "can_test": has_permission(auth, db, TEST_PERMISSION),
            "error": request.query_params.get("error"),
            "saved": request.query_params.get("saved"),
            "test_result": request.query_params.get("test_result"),
            "test_token": str(uuid4()),
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "active_menu": "system",
            "active_page": "customer-occasions",
        },
    )


@router.post(
    "/{occasion}", dependencies=[Depends(require_permission(UPDATE_PERMISSION))]
)
def save_occasion(
    occasion: CustomerOccasion,
    request: Request,
    subject: str = Form(...),
    body: str = Form(...),
    timezone: str = Form(...),
    send_hour: int = Form(...),
    send_minute: int = Form(...),
    revision: int = Form(...),
    enabled: bool = Form(False),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    auth = require_user_auth(request=request, db=db)
    try:
        settings = OccasionSettings(
            occasion=occasion,
            subject=subject,
            body=body,
            timezone=timezone,
            send_hour=send_hour,
            send_minute=send_minute,
            revision=revision,
            enabled=enabled,
        )
        command = SaveOccasionCommand(
            context=CommandContext.system(
                actor=f"admin:{auth['principal_id']}",
                scope=occasion.value,
                reason="Review annual customer greeting configuration",
            ),
            settings=settings,
        )
        db_session_adapter.release_read_transaction(db)
        customer_occasions.save(db=db, command=command)
    except (DomainError, ValidationError) as exc:
        message = (
            exc.message
            if isinstance(exc, DomainError)
            else "Check the message, timezone, and send time."
        )
        return RedirectResponse(
            f"/admin/notifications/occasions?error={quote(message)}", status_code=303
        )
    return RedirectResponse("/admin/notifications/occasions?saved=1", status_code=303)


@router.post(
    "/{occasion}/test", dependencies=[Depends(require_permission(TEST_PERMISSION))]
)
def test_occasion(
    occasion: CustomerOccasion,
    request: Request,
    recipient: EmailStr = Form(...),
    command_id: UUID = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    auth = require_user_auth(request=request, db=db)
    command = TestOccasionCommand(
        context=CommandContext.system(
            actor=f"admin:{auth['principal_id']}",
            scope=occasion.value,
            reason="Explicit operator greeting test",
            command_id=command_id,
        ),
        occasion=occasion,
        recipient=recipient,
    )
    db_session_adapter.release_read_transaction(db)
    try:
        result = customer_occasions.send_test(db=db, command=command)
    except DomainError as exc:
        return RedirectResponse(
            f"/admin/notifications/occasions?error={quote(exc.message)}",
            status_code=303,
        )
    notice = (
        "Test email queued."
        if result.queued
        else "Test email suppressed. Review notification history."
    )
    return RedirectResponse(
        f"/admin/notifications/occasions?test_result={quote(notice)}", status_code=303
    )
